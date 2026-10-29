package main

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Input validation for node registration (docs/design/node-registration.md section 3). This is a
// security boundary: id, hostname, user and port end up in the fleet SSH file and in ssh and
// ssh-keyscan arguments, so every value is matched against a strict whitelist first.

var (
	nodeIDRe     = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	dnsHostRe    = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$`)
	allDigitsRe  = regexp.MustCompile(`^[0-9]+$`)
	nodeUserRe   = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)
	fingerprintR = regexp.MustCompile(`^SHA256:[A-Za-z0-9+/]{43}$`)
	metaTokenRe  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`)
	metaTierRe   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,31}$`)
	metaRoleRe   = regexp.MustCompile(`^[a-z][a-z0-9_]{0,31}$`)
	metaTagRe    = regexp.MustCompile(`^tag:[a-z0-9][a-z0-9-]{0,62}$`)
	archRe       = regexp.MustCompile(`^[A-Za-z0-9_]{1,16}$`)
)

// fieldError is a validation failure tied to a request field, so the UI can mark the input.
type fieldError struct {
	Field string
	Msg   string
}

func (e *fieldError) Error() string { return e.Field + ": " + e.Msg }

func badField(field, format string, a ...any) error {
	return &fieldError{Field: field, Msg: fmt.Sprintf(format, a...)}
}

func validNodeID(id string) bool { return nodeIDRe.MatchString(id) }

// reservedNodeIDs are words the API paths and the dashboard routes use next to a node id
// (/api/nodes/registration, /api/nodes/check, #/nodes/new, .../{id}/probe).
var reservedNodeIDs = map[string]bool{"new": true, "registration": true, "check": true, "probe": true}

func validateNodeID(id string) error {
	if !validNodeID(id) {
		return badField("id", "must match ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase letters, digits and dashes)")
	}
	if reservedNodeIDs[id] {
		return badField("id", "%q is reserved (used by API paths and dashboard routes); choose another id", id)
	}
	return nil
}

// validateHostName accepts a canonical IPv4 address or a DNS name. No whitespace, "%", quotes,
// colons or leading "-" can pass: ssh-keyscan has no "--", so the leading character matters.
func validateHostName(h string) error {
	switch {
	case h == "":
		return badField("hostname", "is required")
	case len(h) > 253:
		return badField("hostname", "is longer than 253 characters")
	case isIPv4Literal(h):
		return nil
	case !dnsHostRe.MatchString(h):
		return badField("hostname", "must be an IPv4 address or a DNS name (letters, digits, dots and dashes)")
	}
	// A name whose last label is numeric is a malformed IPv4 address, not a DNS name.
	if last := h[strings.LastIndexByte(h, '.')+1:]; allDigitsRe.MatchString(last) {
		return badField("hostname", "is not a valid IPv4 address or DNS name")
	}
	return nil
}

func validateUser(u string) error {
	if !nodeUserRe.MatchString(u) {
		return badField("user", "must match ^[a-z_][a-z0-9_-]{0,31}$")
	}
	return nil
}

func validatePort(p int) error {
	if p < 1 || p > 65535 {
		return badField("port", "must be between 1 and 65535")
	}
	return nil
}

func validateFingerprint(fp string) error {
	if !fingerprintR.MatchString(fp) {
		return badField("host_key_fingerprint", "must look like SHA256:<43 base64 characters>")
	}
	return nil
}

// connection is the fleet-file part of a node.
type connection struct {
	HostName string
	User     string
	Port     int
}

func validateConnection(c connection) error {
	if err := validateHostName(c.HostName); err != nil {
		return err
	}
	if err := validateUser(c.User); err != nil {
		return err
	}
	return validatePort(c.Port)
}

// metadataKeys is the fixed, ordered set of inventory fields a dashboard session may set.
var metadataKeys = []string{"tier", "role", "guardian_role", "cli_support", "ops_roles", "tailscale_tags", "description"}

const (
	maxMetaList        = 16
	maxDescriptionRune = 500
)

// errHubRole reports an attempt to change guardian_role "hub" (there is exactly one hub).
var errHubRole = errors.New("guardian_role hub cannot be set or cleared from the dashboard")

// isUnset reports a value that means "no value": null, a blank string or an empty list.
func isUnset(raw any) bool {
	switch v := raw.(type) {
	case nil:
		return true
	case string:
		return strings.TrimSpace(v) == ""
	case []any:
		return len(v) == 0
	}
	return false
}

func hasControl(s string) bool {
	for _, r := range s {
		if unicode.IsControl(r) {
			return true
		}
	}
	return false
}

// validateMetaValue checks one metadata value (string or []any of strings from JSON) and returns
// its normalised form: a trimmed string, or a de-duplicated []string.
func validateMetaValue(key string, raw any) (any, error) {
	str := func(re *regexp.Regexp, what string) (string, error) {
		s, ok := raw.(string)
		if !ok {
			return "", badField("metadata."+key, "must be a string")
		}
		s = strings.TrimSpace(s)
		if !re.MatchString(s) {
			return "", badField("metadata."+key, "%s", what)
		}
		return s, nil
	}
	list := func(re *regexp.Regexp, what string) ([]string, error) {
		items, ok := raw.([]any)
		if !ok {
			return nil, badField("metadata."+key, "must be a list of strings")
		}
		if len(items) > maxMetaList {
			return nil, badField("metadata."+key, "has more than %d entries", maxMetaList)
		}
		out := make([]string, 0, len(items))
		seen := map[string]bool{}
		for _, it := range items {
			s, ok := it.(string)
			if !ok {
				return nil, badField("metadata."+key, "must be a list of strings")
			}
			if s = strings.TrimSpace(s); !re.MatchString(s) {
				return nil, badField("metadata."+key, "%s", what)
			}
			if !seen[s] {
				seen[s] = true
				out = append(out, s)
			}
		}
		return out, nil
	}
	switch key {
	case "tier":
		return str(metaTierRe, "must be 1-32 characters: letters, digits, space and ._:-")
	case "role":
		return str(metaTokenRe, "must be 1-64 characters: letters, digits and ._:-")
	case "guardian_role":
		// Values are "hub" (reserved, exactly one) and "node". Any other token is still accepted:
		// legacy inventories hold "agent", which behaves like "node" (every consumer only tests for
		// "hub"). Restricted after the inventory data migration.
		s, err := str(metaTokenRe, "must be 1-64 characters: letters, digits and ._:-")
		if err != nil {
			return nil, err
		}
		return s, nil
	case "cli_support":
		return list(metaTokenRe, "entries must be 1-64 characters: letters, digits and ._:-")
	case "ops_roles":
		return list(metaRoleRe, "entries must be 1-32 characters: lowercase letters, digits and _, starting with a letter")
	case "tailscale_tags":
		return list(metaTagRe, "entries must look like tag:name (lowercase letters, digits, dashes)")
	case "description":
		s, ok := raw.(string)
		if !ok {
			return nil, badField("metadata.description", "must be a string")
		}
		s = strings.TrimSpace(s)
		if utf8.RuneCountInString(s) > maxDescriptionRune {
			return nil, badField("metadata.description", "is longer than %d characters", maxDescriptionRune)
		}
		if hasControl(s) {
			return nil, badField("metadata.description", "must not contain control characters or line breaks")
		}
		return s, nil
	}
	return nil, badField("metadata."+key, "is not a settable field")
}

// validateMetadata validates a request's metadata object. A nil/empty string/empty list value
// means "unset" and is returned as a nil entry. Unknown keys are rejected so typos are not
// silently dropped. isHub tells whether the node being edited is the hub.
func validateMetadata(in map[string]any, isHub bool) (map[string]any, error) {
	out := map[string]any{}
	for key := range in {
		known := false
		for _, k := range metadataKeys {
			known = known || k == key
		}
		if !known {
			return nil, badField("metadata."+key, "is not a settable field")
		}
	}
	for _, key := range metadataKeys {
		raw, present := in[key]
		if !present {
			continue
		}
		if isUnset(raw) {
			out[key] = nil
		} else {
			v, err := validateMetaValue(key, raw)
			if err != nil {
				return nil, err
			}
			switch t := v.(type) {
			case string:
				if t == "" {
					v = nil
				}
			case []string:
				if len(t) == 0 {
					v = nil
				}
			}
			out[key] = v
		}
		if key == "guardian_role" {
			// The hub keeps its role; nobody else may claim it.
			if s, _ := out[key].(string); (s == "hub") != isHub || (isHub && out[key] == nil) {
				return nil, badField("metadata.guardian_role", "%v", errHubRole)
			}
		}
	}
	return out, nil
}

// nodeFormRules is the machine-readable form of the rules above, served in the "validation" object
// of GET /api/nodes/registration so the dashboard form can mirror the server (the server stays
// authoritative; these strings are the same regular expressions the handlers match). Patterns use
// the RE2 syntax shared by Go and JavaScript for these expressions.
func nodeFormRules() map[string]any {
	list := func(re *regexp.Regexp) map[string]any {
		return map[string]any{"pattern": re.String(), "max_items": maxMetaList, "unique": true}
	}
	return map[string]any{
		"id": map[string]any{"pattern": nodeIDRe.String(), "reserved": []string{"check", "new", "probe", "registration"}},
		"hostname": map[string]any{
			"max_length":      253,
			"ipv4":            "canonical dotted quad, each octet 0-255 without leading zeros",
			"dns_pattern":     dnsHostRe.String(),
			"last_label_rule": "a DNS name whose last label is all digits is rejected (malformed IPv4)",
		},
		"user":                 map[string]any{"pattern": nodeUserRe.String()},
		"port":                 map[string]any{"min": 1, "max": 65535},
		"host_key_fingerprint": map[string]any{"pattern": fingerprintR.String()},
		"metadata": map[string]any{
			"tier":           map[string]any{"pattern": metaTierRe.String()},
			"role":           map[string]any{"pattern": metaTokenRe.String()},
			"guardian_role":  map[string]any{"pattern": metaTokenRe.String(), "default": "node", "hub_reserved": "the value hub cannot be set on a non-hub node or cleared on the hub"},
			"cli_support":    list(metaTokenRe),
			"ops_roles":      list(metaRoleRe),
			"tailscale_tags": list(metaTagRe),
			"description":    map[string]any{"max_runes": maxDescriptionRune, "control_characters": false},
			"blank_means":    "unset (null, blank string or empty list removes the field)",
		},
	}
}
