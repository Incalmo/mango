// mango-proxy records the HTTP(S) downloads of a command, or replays them.
//
//	mango-proxy record OUT [--upstream-ca FILE] -- COMMAND...
//	mango-proxy replay DIR -- COMMAND...
//
// The command runs where every HTTP(S) request reaches this process, whatever
// client makes it. Recording forwards each request upstream and stores the
// response; replaying answers from the stored responses and never opens an
// outbound connection. The proxy knows nothing about any package manager: a
// download is a method, a URL and the bytes that came back.
//
// Requests are captured in two independent ways, so a client needs to honour
// only one of them:
//
//   - Environment: proxy and certificate-authority variables that HTTP clients
//     conventionally read.
//   - Namespace (needs root and CAP_SYS_ADMIN): the command runs in a private
//     mount namespace where every host name resolves to this process and the
//     system certificate bundles contain only its authority. No file of the
//     surrounding system is modified.
package main

import (
	"bufio"
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Entry is one recorded response. Blob names a file under blobs/.
type Entry struct {
	Status  int               `json:"status"`
	Headers map[string]string `json:"headers,omitempty"`
	Blob    string            `json:"sha256,omitempty"`
	Size    int64             `json:"size"`
	// HeadOnly marks a response seen only for HEAD: its body is unknown.
	HeadOnly bool `json:"head_only,omitempty"`
	// Stable reports that the server declared the body long-lived, so the URL
	// can be expected to keep serving these bytes. Recorded for the author's
	// tooling; replay ignores it.
	Stable bool `json:"stable,omitempty"`
}

// stable applies HTTP's own freshness rules (RFC 9111, section 4.2): an
// explicit lifetime, else a tenth of the time since the last modification.
func stable(header http.Header) bool {
	const day = 24 * time.Hour
	lifetime := time.Duration(-1)
	for _, directive := range strings.Split(strings.ToLower(header.Get("Cache-Control")), ",") {
		directive = strings.TrimSpace(directive)
		switch {
		case directive == "immutable":
			return true
		case directive == "no-cache" || directive == "no-store":
			return false
		case strings.HasPrefix(directive, "max-age="):
			if seconds, err := time.ParseDuration(strings.TrimPrefix(directive, "max-age=") + "s"); err == nil {
				lifetime = seconds
			}
		}
	}
	if lifetime < 0 {
		modified, err := http.ParseTime(header.Get("Last-Modified"))
		if err != nil {
			return false
		}
		served, err := http.ParseTime(header.Get("Date"))
		if err != nil {
			served = time.Now()
		}
		lifetime = served.Sub(modified) / 10
	}
	return lifetime >= day
}

// Index is the replayable description of everything a command downloaded.
type Index struct {
	Version int              `json:"version"`
	Entries map[string]Entry `json:"entries"`
}

// Response headers that are not part of the recorded answer: they describe
// one connection, one moment or the serving infrastructure. Everything else
// is kept, because clients give meaning to headers this proxy cannot know.
var volatileHeaders = map[string]bool{
	"Connection": true, "Keep-Alive": true, "Proxy-Authenticate": true, "Trailer": true,
	"Transfer-Encoding": true, "Upgrade": true, "Content-Length": true, "Content-Encoding": true,
	"Accept-Ranges": true, "Content-Range": true, "Date": true, "Age": true, "Expires": true,
	"Set-Cookie": true, "Via": true, "Server": true, "Vary": true, "Alt-Svc": true,
	"Strict-Transport-Security": true, "Report-To": true, "Nel": true, "Server-Timing": true,
}

// Infrastructure headers are named by vendor prefix rather than one by one.
var volatilePrefixes = []string{"X-Cache", "X-Served-By", "X-Timer", "X-Fastly", "X-Amz-", "X-Goog-", "X-Github-", "X-Request", "X-Envoy-", "Cf-", "Fastly-"}

func recordedHeaders(header http.Header) map[string]string {
	kept := map[string]string{}
next:
	for name, values := range header {
		if volatileHeaders[name] {
			continue
		}
		for _, prefix := range volatilePrefixes {
			if strings.HasPrefix(name, prefix) {
				continue next
			}
		}
		kept[name] = strings.Join(values, ", ")
	}
	return kept
}

// Request headers that must not reach upstream: they would make the stored
// response partial, conditional, encoded or dependent on a credential.
var droppedHeaders = []string{
	"Range", "If-Range", "If-None-Match", "If-Modified-Since", "If-Match",
	"If-Unmodified-Since", "Accept-Encoding", "Authorization", "Cookie",
	"Proxy-Authorization", "Proxy-Connection", "Connection", "Keep-Alive",
	"Te", "Trailer", "Transfer-Encoding", "Upgrade",
}

type proxy struct {
	record   bool
	dir      string
	upstream *http.Transport
	ca       *authority

	mu       sync.Mutex
	index    Index
	problems map[string]bool
}

// key identifies a request by what determines its answer: the method, the
// URL and, when it carries one, the digest of its body. A HEAD shares the
// key of its GET.
func key(method string, scheme string, host string, uri string, body []byte) string {
	if method == http.MethodHead {
		method = http.MethodGet
	}
	host = strings.ToLower(host)
	if (scheme == "https" && strings.HasSuffix(host, ":443")) || (scheme == "http" && strings.HasSuffix(host, ":80")) {
		host = host[:strings.LastIndex(host, ":")]
	}
	id := method + " " + scheme + "://" + host + uri
	if len(body) > 0 {
		digest := sha256.Sum256(body)
		id += " " + hex.EncodeToString(digest[:])
	}
	return id
}

func (p *proxy) problem(format string, args ...any) {
	message := fmt.Sprintf(format, args...)
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.problems[message] {
		p.problems[message] = true
		fmt.Fprintln(os.Stderr, "mango-proxy: "+message)
	}
}

// ServeHTTP handles proxy requests: CONNECT tunnels and absolute-URL requests.
func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodConnect {
		p.tunnel(w, r)
		return
	}
	if !r.URL.IsAbs() {
		p.exchange(w, r, "http", r.Host) // captured by name resolution
		return
	}
	p.exchange(w, r, r.URL.Scheme, r.URL.Host)
}

// tunnel terminates TLS for the requested host and serves the requests inside.
func (p *proxy) tunnel(w http.ResponseWriter, r *http.Request) {
	hijacker, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "cannot hijack", http.StatusInternalServerError)
		return
	}
	conn, _, err := hijacker.Hijack()
	if err != nil {
		return
	}
	defer conn.Close()
	if _, err := io.WriteString(conn, "HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		return
	}
	p.serveTLS(conn, r.Host)
}

// serveTLS terminates TLS and answers the requests inside. Without a CONNECT
// target the host comes from the handshake's server name and each request.
func (p *proxy) serveTLS(conn net.Conn, target string) {
	name := ""
	secure := tls.Server(conn, &tls.Config{
		NextProtos: []string{"http/1.1"},
		GetCertificate: func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
			name = hello.ServerName
			if name == "" {
				name, _, _ = net.SplitHostPort(target)
			}
			if name == "" {
				name = "localhost"
			}
			return p.ca.leaf(name)
		},
	})
	if err := secure.Handshake(); err != nil {
		p.problem("TLS handshake failed: %v (does the command trust the proxy's certificate authority?)", err)
		return
	}
	defer secure.Close()
	reader := bufio.NewReader(secure)
	for {
		request, err := http.ReadRequest(reader)
		if err != nil {
			return
		}
		host := target
		if host == "" {
			host = request.Host
		}
		if host == "" {
			host = name
		}
		response := &tunnelWriter{conn: secure, header: http.Header{}, head: request.Method == http.MethodHead}
		p.exchange(response, request, "https", host)
		request.Body.Close()
		if response.finish() != nil || request.Close {
			return
		}
	}
}

// tunnelWriter writes one HTTP/1.1 response with an explicit length.
type tunnelWriter struct {
	conn   net.Conn
	header http.Header
	status int
	body   []byte
	head   bool
}

func (t *tunnelWriter) Header() http.Header { return t.header }
func (t *tunnelWriter) WriteHeader(status int) {
	if t.status == 0 {
		t.status = status
	}
}
func (t *tunnelWriter) Write(data []byte) (int, error) {
	t.WriteHeader(http.StatusOK)
	t.body = append(t.body, data...)
	return len(data), nil
}
func (t *tunnelWriter) finish() error {
	t.WriteHeader(http.StatusOK)
	if t.header.Get("Content-Length") == "" {
		t.header.Set("Content-Length", fmt.Sprint(len(t.body)))
	}
	buffer := bufio.NewWriter(t.conn)
	fmt.Fprintf(buffer, "HTTP/1.1 %d %s\r\n", t.status, http.StatusText(t.status))
	t.header.Write(buffer)
	buffer.WriteString("\r\n")
	if !t.head {
		buffer.Write(t.body)
	}
	return buffer.Flush()
}

// exchange answers one request from upstream (recording) or from the index.
func (p *proxy) exchange(w http.ResponseWriter, r *http.Request, scheme string, host string) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	id := key(r.Method, scheme, host, r.URL.RequestURI(), body)
	if !p.record {
		p.mu.Lock()
		entry, ok := p.index.Entries[id]
		p.mu.Unlock()
		if !ok || (entry.HeadOnly && r.Method != http.MethodHead) {
			p.problem("not recorded: %s", id)
			http.Error(w, "mango-proxy: not recorded: "+id, http.StatusBadGateway)
			return
		}
		p.reply(w, r, entry)
		return
	}
	outbound, err := http.NewRequest(r.Method, scheme+"://"+host+r.URL.RequestURI(), bytes.NewReader(body))
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	outbound.Header = r.Header.Clone()
	for _, name := range droppedHeaders {
		outbound.Header.Del(name)
	}
	response, err := p.upstream.RoundTrip(outbound)
	if err != nil {
		p.problem("upstream request failed: %s: %v", id, err)
		http.Error(w, "mango-proxy: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer response.Body.Close()
	entry := Entry{Status: response.StatusCode, Headers: recordedHeaders(response.Header)}
	replayable := true
	// Server errors are transient: pass them on, but never freeze them.
	if response.StatusCode >= 500 || response.StatusCode == http.StatusTooManyRequests {
		replayable = false
	}
	if r.Method == http.MethodHead {
		// A HEAD shares its GET's entry; only keep it when no body is known yet.
		entry.HeadOnly = true
		if response.ContentLength > 0 {
			entry.Size = response.ContentLength
		}
		p.mu.Lock()
		_, known := p.index.Entries[id]
		p.mu.Unlock()
		if !known && replayable {
			p.store(id, entry)
		}
		p.reply(w, r, entry)
		return
	}
	blob, size, err := p.save(response.Body)
	if err != nil {
		p.problem("download failed: %s: %v", id, err)
		http.Error(w, "mango-proxy: "+err.Error(), http.StatusBadGateway)
		return
	}
	entry.Blob, entry.Size = blob, size
	entry.Stable = response.StatusCode == http.StatusOK && stable(response.Header)
	if replayable {
		p.store(id, entry)
	}
	p.reply(w, r, entry)
}

func (p *proxy) store(id string, entry Entry) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.index.Entries[id] = entry
}

// save streams a body into blobs/<sha256> and returns that name.
func (p *proxy) save(body io.Reader) (string, int64, error) {
	file, err := os.CreateTemp(filepath.Join(p.dir, "blobs"), ".partial-")
	if err != nil {
		return "", 0, err
	}
	defer os.Remove(file.Name())
	digest := sha256.New()
	size, err := io.Copy(io.MultiWriter(file, digest), body)
	if closeErr := file.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return "", 0, err
	}
	name := hex.EncodeToString(digest.Sum(nil))
	return name, size, os.Rename(file.Name(), filepath.Join(p.dir, "blobs", name))
}

func (p *proxy) reply(w http.ResponseWriter, r *http.Request, entry Entry) {
	for name, value := range entry.Headers {
		w.Header().Set(name, value)
	}
	w.Header().Set("Content-Length", fmt.Sprint(entry.Size))
	w.WriteHeader(entry.Status)
	if r.Method == http.MethodHead || entry.Blob == "" {
		return
	}
	file, err := os.Open(filepath.Join(p.dir, "blobs", entry.Blob))
	if err != nil {
		p.problem("missing recorded body %s: %v", entry.Blob, err)
		return
	}
	defer file.Close()
	io.Copy(w, file)
}

func (p *proxy) writeIndex() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	data, err := json.MarshalIndent(p.index, "", " ") // map keys are sorted
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(p.dir, "index.json"), append(data, '\n'), 0o644)
}

// answerDNS resolves every name to this host, so clients connect to the proxy.
func answerDNS(query []byte) []byte {
	if len(query) < 12 || query[2]&0x80 != 0 || binary.BigEndian.Uint16(query[4:]) != 1 {
		return nil
	}
	end := 12
	for end < len(query) && query[end] != 0 {
		end += int(query[end]) + 1
	}
	end += 5 // root label, type, class
	if end > len(query) {
		return nil
	}
	kind := binary.BigEndian.Uint16(query[end-4:])
	reply := append([]byte{}, query[:end]...)
	reply[2] = 0x84 | query[2]&0x01 // response, authoritative, recursion as asked
	reply[3] = 0x80
	binary.BigEndian.PutUint16(reply[6:], 0)
	binary.BigEndian.PutUint16(reply[8:], 0)
	binary.BigEndian.PutUint16(reply[10:], 0)
	if kind == 1 { // A; other types get an empty answer so clients use IPv4
		binary.BigEndian.PutUint16(reply[6:], 1)
		reply = append(reply, 0xC0, 12, 0, 1, 0, 1, 0, 0, 0, 0, 0, 4, 127, 0, 0, 1)
	}
	return reply
}

// capture serves DNS, HTTP and HTTPS on this host's standard ports. It fails
// without the privilege to bind them, which leaves the environment mechanism.
func (p *proxy) capture(server *http.Server) (func(), error) {
	dns, err := net.ListenPacket("udp", "127.0.0.1:53")
	if err != nil {
		return nil, err
	}
	plain, err := net.Listen("tcp", "127.0.0.1:80")
	if err != nil {
		dns.Close()
		return nil, err
	}
	secure, err := net.Listen("tcp", "127.0.0.1:443")
	if err != nil {
		dns.Close()
		plain.Close()
		return nil, err
	}
	go func() {
		buffer := make([]byte, 1500)
		for {
			size, from, err := dns.ReadFrom(buffer)
			if err != nil {
				return
			}
			if reply := answerDNS(buffer[:size]); reply != nil {
				dns.WriteTo(reply, from)
			}
		}
	}()
	go server.Serve(plain)
	go func() {
		for {
			conn, err := secure.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				p.serveTLS(conn, "")
			}()
		}
	}()
	return func() { dns.Close(); plain.Close(); secure.Close() }, nil
}

// Where Linux distributions keep the system certificate bundle.
var bundles = []string{
	"/etc/ssl/certs/ca-certificates.crt",
	"/etc/pki/tls/certs/ca-bundle.crt",
	"/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
	"/etc/ssl/ca-bundle.pem",
	"/etc/pki/tls/cacert.pem",
	"/etc/ssl/cert.pem",
}

// enterNamespace runs in the command's private mount namespace: it overlays
// name resolution and the certificate bundles, then becomes the command.
func enterNamespace(arguments []string) error {
	resolver, authority, command := arguments[0], arguments[1], arguments[2:]
	if err := syscall.Mount("", "/", "", syscall.MS_REC|syscall.MS_PRIVATE, ""); err != nil {
		return err
	}
	overlay := func(source string, target string) error {
		resolved, err := filepath.EvalSymlinks(target)
		if err != nil {
			return nil // nothing to overlay
		}
		return syscall.Mount(source, resolved, "", syscall.MS_BIND, "")
	}
	if err := overlay(resolver, "/etc/resolv.conf"); err != nil {
		return err
	}
	for _, bundle := range bundles {
		if err := overlay(authority, bundle); err != nil {
			return err
		}
	}
	path, err := exec.LookPath(command[0])
	if err != nil {
		return err
	}
	return syscall.Exec(path, command, os.Environ())
}

// authority is a certificate authority that exists only for one command.
type authority struct {
	certificate *x509.Certificate
	key         *ecdsa.PrivateKey
	der         []byte
	mu          sync.Mutex
	leaves      map[string]*tls.Certificate
}

func newAuthority() (*authority, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "mango-proxy build-local authority"},
		NotBefore:             time.Now().Add(-24 * time.Hour),
		NotAfter:              time.Now().Add(30 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return nil, err
	}
	certificate, err := x509.ParseCertificate(der)
	return &authority{certificate: certificate, key: key, der: der, leaves: map[string]*tls.Certificate{}}, err
}

func (a *authority) leaf(host string) (*tls.Certificate, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if leaf, ok := a.leaves[host]; ok {
		return leaf, nil
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	template := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: host},
		NotBefore:    a.certificate.NotBefore,
		NotAfter:     a.certificate.NotAfter,
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	if ip := net.ParseIP(host); ip != nil {
		template.IPAddresses = []net.IP{ip}
	} else {
		template.DNSNames = []string{host}
	}
	der, err := x509.CreateCertificate(rand.Reader, template, a.certificate, &key.PublicKey, a.key)
	if err != nil {
		return nil, err
	}
	leaf := &tls.Certificate{Certificate: [][]byte{der, a.der}, PrivateKey: key}
	a.leaves[host] = leaf
	return leaf, nil
}

func (a *authority) pem() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: a.der})
}

// jks encodes the authority as a Java KeyStore holding one trusted certificate.
func (a *authority) jks(password string) []byte {
	var out []byte
	u16 := func(v int) { out = binary.BigEndian.AppendUint16(out, uint16(v)) }
	u32 := func(v int) { out = binary.BigEndian.AppendUint32(out, uint32(v)) }
	text := func(s string) { u16(len(s)); out = append(out, s...) }
	u32(0xFEEDFEED)
	u32(2) // format version
	u32(1) // entries
	u32(2) // trusted certificate entry
	text("mango-proxy")
	out = binary.BigEndian.AppendUint64(out, uint64(a.certificate.NotBefore.UnixMilli()))
	text("X.509")
	u32(len(a.der))
	out = append(out, a.der...)
	digest := sha1.New()
	for _, r := range password {
		digest.Write([]byte{byte(r >> 8), byte(r)})
	}
	digest.Write([]byte("Mighty Aphrodite"))
	digest.Write(out)
	return digest.Sum(out)
}

// environment points every common HTTP client at the proxy and its authority.
// Variables are generic client conventions, not package-manager settings.
func environment(address string, pemPath string, jksPath string) []string {
	url := "http://" + address
	host, port, _ := net.SplitHostPort(address)
	env := os.Environ()
	set := func(name string, value string) { env = append(env, name+"="+value) }
	for _, name := range []string{"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"} {
		set(name, url)
	}
	set("NO_PROXY", "")
	set("no_proxy", "")
	for _, name := range []string{
		"SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO",
		"NIX_SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "PIP_CERT", "CARGO_HTTP_CAINFO",
		"HEX_CACERTS_PATH", "DENO_CERT", "AWS_CA_BUNDLE", "PERL_LWP_SSL_CA_FILE",
		"SYSTEM_CERTIFICATE_PATH", "npm_config_cafile", "COMPOSER_CAFILE",
	} {
		set(name, pemPath)
	}
	java := fmt.Sprintf(
		"-Dhttp.proxyHost=%s -Dhttp.proxyPort=%s -Dhttps.proxyHost=%s -Dhttps.proxyPort=%s "+
			"-Dhttp.nonProxyHosts= -Djavax.net.ssl.trustStore=%s -Djavax.net.ssl.trustStorePassword=changeit "+
			"-Djavax.net.ssl.trustStoreType=JKS",
		host, port, host, port, jksPath)
	if existing := os.Getenv("JAVA_TOOL_OPTIONS"); existing != "" {
		java = existing + " " + java
	}
	set("JAVA_TOOL_OPTIONS", java)
	set("MANGO_PROXY_URL", url)
	set("MANGO_PROXY_CA", pemPath)
	set("MANGO_PROXY_JKS", jksPath)
	return env
}

func run(arguments []string) (int, error) {
	usage := errors.New("usage: mango-proxy record OUT [--upstream-ca FILE] -- COMMAND... | mango-proxy replay DIR -- COMMAND...")
	if len(arguments) == 3 && arguments[0] == "--chown" {
		// Hand a recording made as root to its author, in images without tools.
		var uid, gid int
		if _, err := fmt.Sscanf(arguments[1], "%d:%d", &uid, &gid); err != nil {
			return 2, usage
		}
		return 0, filepath.Walk(arguments[2], func(path string, _ os.FileInfo, err error) error {
			if err != nil {
				return err
			}
			return os.Lchown(path, uid, gid)
		})
	}
	if len(arguments) > 3 && arguments[0] == "--in-namespace" {
		return 127, enterNamespace(arguments[1:])
	}
	if len(arguments) < 4 || (arguments[0] != "record" && arguments[0] != "replay") {
		return 2, usage
	}
	p := &proxy{record: arguments[0] == "record", dir: arguments[1], problems: map[string]bool{}}
	p.index = Index{Version: 1, Entries: map[string]Entry{}}
	rest := arguments[2:]
	roots := ""
	if p.record && rest[0] == "--upstream-ca" && len(rest) > 2 {
		roots, rest = rest[1], rest[2:]
	}
	if rest[0] != "--" || len(rest) < 2 {
		return 2, usage
	}
	command := rest[1:]
	if p.record {
		if err := os.MkdirAll(filepath.Join(p.dir, "blobs"), 0o755); err != nil {
			return 1, err
		}
		pool, err := x509.SystemCertPool()
		if roots != "" {
			data, readErr := os.ReadFile(roots)
			if readErr != nil {
				return 1, readErr
			}
			pool = x509.NewCertPool()
			if !pool.AppendCertsFromPEM(data) {
				return 1, fmt.Errorf("%s contains no certificates", roots)
			}
		} else if err != nil {
			return 1, err
		}
		p.upstream = &http.Transport{
			Proxy:               http.ProxyFromEnvironment,
			TLSClientConfig:     &tls.Config{RootCAs: pool},
			TLSHandshakeTimeout: 30 * time.Second,
			ForceAttemptHTTP2:   true,
		}
	} else {
		data, err := os.ReadFile(filepath.Join(p.dir, "index.json"))
		if err != nil {
			return 1, err
		}
		if err := json.Unmarshal(data, &p.index); err != nil || p.index.Version != 1 {
			return 1, fmt.Errorf("%s/index.json: unsupported recording", p.dir)
		}
	}
	var err error
	if p.ca, err = newAuthority(); err != nil {
		return 1, err
	}
	scratch, err := os.MkdirTemp("", "mango-proxy-")
	if err != nil {
		return 1, err
	}
	defer os.RemoveAll(scratch)
	pemPath, jksPath := filepath.Join(scratch, "ca.pem"), filepath.Join(scratch, "ca.jks")
	if err := os.WriteFile(pemPath, p.ca.pem(), 0o644); err != nil {
		return 1, err
	}
	if err := os.WriteFile(jksPath, p.ca.jks("changeit"), 0o644); err != nil {
		return 1, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 1, err
	}
	server := &http.Server{Handler: p}
	go server.Serve(listener)
	defer server.Close()

	child := exec.Command(command[0], command[1:]...)
	// Capture by name resolution when privileged; a file that only the command
	// sees replaces /etc/resolv.conf, which must exist to be overlaid.
	if _, missing := os.Stat("/etc/resolv.conf"); missing == nil {
		if stop, err := p.capture(server); err == nil {
			defer stop()
			resolver := filepath.Join(scratch, "resolv.conf")
			if err := os.WriteFile(resolver, []byte("nameserver 127.0.0.1\n"), 0o644); err != nil {
				return 1, err
			}
			self, err := os.Executable()
			if err != nil {
				return 1, err
			}
			child = exec.Command(self, append([]string{"--in-namespace", resolver, pemPath}, command...)...)
			child.SysProcAttr = &syscall.SysProcAttr{Unshareflags: syscall.CLONE_NEWNS}
		}
	}
	start := func(child *exec.Cmd) error {
		child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
		child.Env = environment(listener.Addr().String(), pemPath, jksPath)
		return child.Start()
	}
	if err := start(child); err != nil {
		if child.SysProcAttr == nil {
			return 127, err
		}
		// No privilege for a private namespace: rely on the environment alone.
		child = exec.Command(command[0], command[1:]...)
		if err := start(child); err != nil {
			return 127, err
		}
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	go func() {
		for received := range signals {
			child.Process.Signal(received)
		}
	}()
	status := 0
	if err := child.Wait(); err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) {
			return 1, err
		}
		status = exit.ExitCode()
		if status < 0 {
			status = 1
		}
	}
	if p.record {
		if err := p.writeIndex(); err != nil {
			return 1, err
		}
	}
	return status, nil
}

func main() {
	status, err := run(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "mango-proxy:", err)
	}
	os.Exit(status)
}
