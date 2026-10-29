package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Read-modify-write access to the hub's servers.json that keeps what the agent does not know
// about: unchanged entries stay as they are, and a changed entry keeps its key order and every
// field it had (recommended, backup_replica, group, ...), because the Python CLI and the
// scripts read fields this package does not model.

// jsonObject is a JSON object that remembers key order and keeps values as raw JSON.
type jsonObject struct {
	keys []string
	vals map[string]json.RawMessage
}

func parseJSONObject(raw json.RawMessage) (*jsonObject, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, errors.New("entry is not a JSON object")
	}
	o := &jsonObject{vals: map[string]json.RawMessage{}}
	for dec.More() {
		kt, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, ok := kt.(string)
		if !ok {
			return nil, errors.New("bad object key")
		}
		var v json.RawMessage
		if err := dec.Decode(&v); err != nil {
			return nil, err
		}
		if _, dup := o.vals[key]; !dup {
			o.keys = append(o.keys, key)
		}
		o.vals[key] = v
	}
	return o, nil
}

// marshalNoEscape is json.Marshal without HTML escaping, so "<" in a description stays readable.
func marshalNoEscape(v any) (json.RawMessage, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

func (o *jsonObject) set(key string, v any) error {
	raw, err := marshalNoEscape(v)
	if err != nil {
		return err
	}
	if _, exists := o.vals[key]; !exists {
		o.keys = append(o.keys, key)
	}
	o.vals[key] = raw
	return nil
}

func (o *jsonObject) del(key string) {
	if _, exists := o.vals[key]; !exists {
		return
	}
	delete(o.vals, key)
	for i, k := range o.keys {
		if k == key {
			o.keys = append(o.keys[:i], o.keys[i+1:]...)
			break
		}
	}
}

func (o *jsonObject) str(key string) string {
	var s string
	if raw, ok := o.vals[key]; ok {
		_ = json.Unmarshal(raw, &s)
	}
	return s
}

func (o *jsonObject) raw() json.RawMessage {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, k := range o.keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		kb, _ := marshalNoEscape(k)
		buf.Write(kb)
		buf.WriteByte(':')
		buf.Write(o.vals[k])
	}
	buf.WriteByte('}')
	return buf.Bytes()
}

// inventory is servers.json held as an array of raw entries.
type inventory struct {
	path    string
	entries []json.RawMessage
	newline bool
}

// inventoryWritePath returns the servers.json the agent may modify. Registration writes only
// when GUARDIAN_SERVERS_FILE names it: without it the agent would fall back to a
// configs/servers.json inside its checkout, a local snapshot that is not the hub's inventory.
func inventoryWritePath() (string, error) {
	p := strings.TrimSpace(os.Getenv("GUARDIAN_SERVERS_FILE"))
	if p == "" {
		return "", &apiError{Status: 409, Code: "inventory_not_configured",
			Msg:  "GUARDIAN_SERVERS_FILE is not set, so the node inventory is read-only",
			Hint: "run scripts/install-agent-service.sh <hub> --hub, which sets GUARDIAN_SERVERS_FILE=~/.guardian/data/servers.json"}
	}
	abs, err := filepath.Abs(p)
	if err != nil {
		return "", err
	}
	if st, err := os.Stat(abs); err != nil || st.IsDir() {
		return "", &apiError{Status: 409, Code: "inventory_missing", Msg: "the inventory file " + abs + " does not exist",
			Hint: "run scripts/install-agent-service.sh <hub> --hub to seed it from the repository copy"}
	}
	return abs, nil
}

func loadInventory(path string) (*inventory, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("servers.json not readable: %w", err)
	}
	inv := &inventory{path: path, newline: len(data) == 0 || data[len(data)-1] == '\n'}
	if err := json.Unmarshal(data, &inv.entries); err != nil {
		return nil, fmt.Errorf("servers.json is not a JSON array: %w", err)
	}
	return inv, nil
}

// idName returns an entry's id and name without parsing the rest.
func idName(raw json.RawMessage) (id, name string) {
	var e struct {
		ID   string `json:"id"`
		Name string `json:"name"`
	}
	_ = json.Unmarshal(raw, &e)
	return e.ID, e.Name
}

// index returns the entry whose id is id, or -1.
func (inv *inventory) index(id string) int {
	for i, raw := range inv.entries {
		if eid, _ := idName(raw); eid == id {
			return i
		}
	}
	return -1
}

// taken reports whether id is already some entry's id or name.
func (inv *inventory) taken(id string) bool {
	for _, raw := range inv.entries {
		if eid, name := idName(raw); eid == id || name == id {
			return true
		}
	}
	return false
}

func (inv *inventory) object(i int) (*jsonObject, error) { return parseJSONObject(inv.entries[i]) }

func (inv *inventory) replace(i int, o *jsonObject) { inv.entries[i] = o.raw() }

func (inv *inventory) append(o *jsonObject) { inv.entries = append(inv.entries, o.raw()) }

func (inv *inventory) remove(i int) { inv.entries = append(inv.entries[:i], inv.entries[i+1:]...) }

// bytes renders the file: two-space indentation like configs/servers.example.json, original trailing
// newline state kept.
func (inv *inventory) bytes() ([]byte, error) {
	// Joined by hand: json.Marshal would HTML-escape the raw entries ("&" -> \u0026).
	var compact bytes.Buffer
	compact.WriteByte('[')
	for i, e := range inv.entries {
		if i > 0 {
			compact.WriteByte(',')
		}
		compact.Write(e)
	}
	compact.WriteByte(']')
	var out bytes.Buffer
	if err := json.Indent(&out, compact.Bytes(), "", "  "); err != nil {
		return nil, err
	}
	if inv.newline {
		out.WriteByte('\n')
	}
	return out.Bytes(), nil
}

func (inv *inventory) save() error {
	data, err := inv.bytes()
	if err != nil {
		return err
	}
	return writeFileAtomic(inv.path, data, true)
}
