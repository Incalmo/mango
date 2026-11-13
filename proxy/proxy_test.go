package main

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// serve starts a proxy over dir and returns a client that uses and trusts it.
func serve(t *testing.T, p *proxy) *http.Client {
	t.Helper()
	var err error
	if p.ca, err = newAuthority(); err != nil {
		t.Fatal(err)
	}
	p.problems = map[string]bool{}
	if p.index.Entries == nil {
		p.index = Index{Version: 1, Entries: map[string]Entry{}}
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: p}
	go server.Serve(listener)
	t.Cleanup(func() { server.Close() })
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(p.ca.pem())
	address, _ := url.Parse("http://" + listener.Addr().String())
	return &http.Client{
		Transport:     &http.Transport{Proxy: http.ProxyURL(address), TLSClientConfig: &tls.Config{RootCAs: pool}},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func get(t *testing.T, client *http.Client, method string, target string) (int, string, http.Header) {
	t.Helper()
	request, _ := http.NewRequest(method, target, nil)
	request.Header.Set("Range", "bytes=0-1")
	request.Header.Set("Authorization", "secret")
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(response.Body)
	return response.StatusCode, string(body), response.Header
}

func post(t *testing.T, client *http.Client, target string, body string) (int, string, http.Header) {
	t.Helper()
	response, err := client.Post(target, "application/octet-stream", bytes.NewReader([]byte(body)))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	answer, _ := io.ReadAll(response.Body)
	return response.StatusCode, string(answer), response.Header
}

func TestRecordThenReplayWithoutUpstream(t *testing.T) {
	failing := true
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Range") != "" || r.Header.Get("Authorization") != "" {
			t.Errorf("request header leaked upstream: %v", r.Header)
		}
		switch r.URL.Path {
		case "/artifact-1.0.jar":
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
			w.Header().Set("Content-Type", "application/java-archive")
			io.WriteString(w, "jar bytes")
		case "/rpc":
			body, _ := io.ReadAll(r.Body)
			w.Header().Set("X-Capability", "kept")
			w.Header().Set("X-Cache-Hits", "dropped")
			io.WriteString(w, "answer to "+string(body))
		case "/index":
			w.Header().Set("Cache-Control", "max-age=60")
			io.WriteString(w, "mutable index "+r.URL.RawQuery)
		case "/moved":
			http.Redirect(w, r, "/artifact-1.0.jar", http.StatusFound)
		case "/flaky":
			if failing {
				failing = false
				http.Error(w, "busy", http.StatusServiceUnavailable)
				return
			}
			io.WriteString(w, "recovered")
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	dir := t.TempDir()
	os.Mkdir(filepath.Join(dir, "blobs"), 0o755)
	pool := x509.NewCertPool()
	pool.AddCert(upstream.Certificate())
	recorder := &proxy{record: true, dir: dir, upstream: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}}
	client := serve(t, recorder)
	paths := []string{"/artifact-1.0.jar", "/index?page=2", "/moved", "/missing", "/flaky", "/flaky"}
	want := map[string]int{}
	bodies := map[string]string{}
	for _, path := range paths {
		status, body, _ := get(t, client, http.MethodGet, upstream.URL+path)
		want[path], bodies[path] = status, body
	}
	if status, _, _ := get(t, client, http.MethodHead, upstream.URL+"/index?probe"); status != http.StatusOK {
		t.Errorf("HEAD while recording: %d", status)
	}
	if _, body, _ := post(t, client, upstream.URL+"/rpc", "want a"); body != "answer to want a" {
		t.Errorf("request body was not forwarded: %q", body)
	}
	post(t, client, upstream.URL+"/rpc", "want b")
	if err := recorder.writeIndex(); err != nil {
		t.Fatal(err)
	}
	upstream.Close()

	var index Index
	data, _ := os.ReadFile(filepath.Join(dir, "index.json"))
	if err := json.Unmarshal(data, &index); err != nil {
		t.Fatal(err)
	}
	artifact := index.Entries["GET "+upstream.URL+"/artifact-1.0.jar"]
	if !artifact.Stable || artifact.Size != 9 || artifact.Headers["Content-Type"] != "application/java-archive" {
		t.Errorf("unexpected artifact entry: %+v", artifact)
	}
	if index.Entries["GET "+upstream.URL+"/index?page=2"].Stable {
		t.Error("a short-lived response must not be stable")
	}
	if len(index.Entries) != 8 {
		t.Errorf("expected 8 replayable entries, got %d: %v", len(index.Entries), index.Entries)
	}

	replayer := &proxy{dir: dir, index: index}
	client = serve(t, replayer)
	for _, path := range paths {
		status, body, header := get(t, client, http.MethodGet, upstream.URL+path)
		if path == "/flaky" {
			if status != http.StatusOK || body != "recovered" {
				t.Errorf("a transient failure was frozen: %d %q", status, body)
			}
			continue
		}
		if status != want[path] || body != bodies[path] {
			t.Errorf("%s replayed as %d %q, recorded %d %q", path, status, body, want[path], bodies[path])
		}
		if path == "/moved" && header.Get("Location") != "/artifact-1.0.jar" {
			t.Errorf("redirect target lost: %v", header)
		}
	}
	if status, body, _ := get(t, client, http.MethodHead, upstream.URL+"/artifact-1.0.jar"); status != http.StatusOK || body != "" {
		t.Errorf("HEAD of a recorded GET: %d %q", status, body)
	}
	if status, _, _ := get(t, client, http.MethodHead, upstream.URL+"/index?probe"); status != http.StatusOK {
		t.Errorf("a recorded HEAD must replay, got %d", status)
	}
	for _, path := range []string{"/index", "/never", "/index?probe"} {
		if status, _, _ := get(t, client, http.MethodGet, upstream.URL+path); status != http.StatusBadGateway {
			t.Errorf("%s was not recorded but replayed with %d", path, status)
		}
	}
	for _, want := range []string{"want b", "want a"} {
		status, body, header := post(t, client, upstream.URL+"/rpc", want)
		if status != http.StatusOK || body != "answer to "+want {
			t.Errorf("request with body %q replayed as %d %q", want, status, body)
		}
		if header.Get("X-Capability") != "kept" || header.Get("X-Cache-Hits") != "" {
			t.Errorf("unexpected replayed headers: %v", header)
		}
	}
	if status, _, _ := post(t, client, upstream.URL+"/rpc", "want c"); status != http.StatusBadGateway {
		t.Errorf("an unrecorded request body must not be answered, got %d", status)
	}
}

func TestStableFollowsHTTPFreshness(t *testing.T) {
	now := time.Now().UTC()
	old := now.Add(-365 * 24 * time.Hour).Format(http.TimeFormat)
	recent := now.Add(-2 * time.Hour).Format(http.TimeFormat)
	for _, c := range []struct {
		header map[string]string
		want   bool
	}{
		{map[string]string{"Cache-Control": "public, immutable, max-age=5"}, true},
		{map[string]string{"Cache-Control": "max-age=86400"}, true},
		{map[string]string{"Cache-Control": "public, max-age=10800"}, false},
		{map[string]string{"Cache-Control": "max-age=0, no-cache", "Last-Modified": old}, false},
		{map[string]string{"Last-Modified": old}, true},
		{map[string]string{"Last-Modified": recent}, false},
		{map[string]string{}, false},
	} {
		header := http.Header{}
		for name, value := range c.header {
			header.Set(name, value)
		}
		if got := stable(header); got != c.want {
			t.Errorf("stable(%v) = %v, want %v", c.header, got, c.want)
		}
	}
}

func TestEveryNameResolvesToTheProxy(t *testing.T) {
	query := []byte{0xAB, 0xCD, 0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0, 7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'o', 'r', 'g', 0, 0, 1, 0, 1}
	reply := answerDNS(query)
	if !bytes.Equal(reply[:2], query[:2]) || reply[2]&0x80 == 0 || binary.BigEndian.Uint16(reply[6:]) != 1 {
		t.Fatalf("unexpected reply header: %v", reply[:12])
	}
	if !bytes.Equal(reply[len(reply)-4:], []byte{127, 0, 0, 1}) {
		t.Errorf("answer is not loopback: %v", reply)
	}
	query[len(query)-3] = 28 // AAAA
	if reply := answerDNS(query); binary.BigEndian.Uint16(reply[6:]) != 0 {
		t.Error("IPv6 queries must get an empty answer")
	}
	if answerDNS(query[:8]) != nil {
		t.Error("truncated queries must be ignored")
	}
}

func TestJavaKeyStoreLayout(t *testing.T) {
	ca, err := newAuthority()
	if err != nil {
		t.Fatal(err)
	}
	store := ca.jks("changeit")
	if binary.BigEndian.Uint32(store) != 0xFEEDFEED || binary.BigEndian.Uint32(store[4:]) != 2 {
		t.Error("not a version 2 Java KeyStore")
	}
	if !bytes.Contains(store, ca.der) || len(store) < len(ca.der)+20 {
		t.Error("keystore does not hold the authority certificate and digest")
	}
}
