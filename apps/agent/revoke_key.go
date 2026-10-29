package main

import (
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	revokeTimeoutSec = 20
	revokeNote       = "After this the hub can no longer reach the node over SSH; the next step is to delete the node."
)

var (
	keyTypeRe = regexp.MustCompile(`^[a-z0-9@.-]{3,64}$`)
	keyBodyRe = regexp.MustCompile(`^[A-Za-z0-9+/]{16,1000}={0,2}$`)
	removedRe = regexp.MustCompile(`(?m)^removed=(\d+)$`)
)

// revokeScript is fixed text. The only substitution is the match string "<type> <base64 body>",
// validated by hubKeyMatch against keyTypeRe/keyBodyRe, so it holds no quote or shell metacharacter.
// grep -F treats it as a literal; the file is rewritten via a copy (cp -p keeps owner and mode).
const revokeScript = `set -u
f="$HOME/.ssh/authorized_keys"
k='%s'
[ -f "$f" ] || { echo removed=0; exit 0; }
n=$(grep -cF -- "$k" "$f" || true)
if [ "${n:-0}" = 0 ]; then echo removed=0; exit 0; fi
ts=$(date +%%Y%%m%%d%%H%%M%%S)
cp -p "$f" "$f.guardian-bak-$ts" || exit 3
tmp="$f.guardian-tmp-$$"
cp -p "$f" "$tmp" || exit 3
grep -vF -- "$k" "$f" > "$tmp" || true
mv "$tmp" "$f" || { rm -f "$tmp"; exit 3; }
echo removed=$n
`

// hubKeyMatch reads the hub fleet public key (the same file authorizedKeyLine installs from) and
// returns the exact "<type> <body>" text that is matched on the node plus its SHA256 fingerprint.
func hubKeyMatch() (match, fingerprint string, err error) {
	data, exists, rerr := readFileIfExists(fleetPubKeyPath())
	if rerr != nil || !exists {
		return "", "", &apiError{Status: http.StatusConflict, Code: "hub_key_missing",
			Msg: "the hub fleet public key " + fleetPubKeyPath() + " is missing; run scripts/setup-hub-ssh.sh"}
	}
	fields := strings.Fields(strings.SplitN(string(data), "\n", 2)[0])
	if len(fields) < 2 || !keyTypeRe.MatchString(fields[0]) || !keyBodyRe.MatchString(fields[1]) {
		return "", "", &apiError{Status: http.StatusConflict, Code: "hub_key_invalid",
			Msg: "the hub fleet public key " + fleetPubKeyPath() + " is not a usable public key line"}
	}
	raw, derr := base64.StdEncoding.DecodeString(fields[1])
	if derr != nil {
		return "", "", &apiError{Status: http.StatusConflict, Code: "hub_key_invalid", Msg: "the hub fleet public key body is not valid base64"}
	}
	sum := sha256.Sum256(raw)
	return fields[0] + " " + fields[1], "SHA256:" + base64.RawStdEncoding.EncodeToString(sum[:]), nil
}

func nodeTarget(s FleetNode) string {
	t := s.User + "@" + s.Hostname
	if s.Port != 0 && s.Port != 22 {
		t += ":" + strconv.Itoa(s.Port)
	}
	return t
}

func findNode(id string) (FleetNode, error) {
	nodes, err := loadNodesWithConnection()
	if err != nil {
		return FleetNode{}, err
	}
	for _, s := range nodes {
		if s.ID == id {
			return s, nil
		}
	}
	return FleetNode{}, &apiError{Status: http.StatusNotFound, Code: "not_found", Msg: "unknown node " + id}
}

// errRevokeHub refuses revoking the hub's key on the hub itself: that would cut hub-to-hub ssh.
var errRevokeHub = &apiError{Status: http.StatusBadRequest, Code: "hub_protected",
	Msg: "refusing to revoke the hub's key on the hub itself"}

// markHubKeyRevoked stores hub_key_revoked_at on the inventory entry. The caller holds lockFleet.
func markHubKeyRevoked(id string, at time.Time) error {
	invPath, err := inventoryWritePath()
	if err != nil {
		return err
	}
	inv, err := loadInventory(invPath)
	if err != nil {
		return err
	}
	i := inv.index(id)
	if i < 0 {
		return fmt.Errorf("node %s is not in the inventory", id)
	}
	obj, err := inv.object(i)
	if err != nil {
		return err
	}
	if err := obj.set("hub_key_revoked_at", at.UTC().Format(time.RFC3339)); err != nil {
		return err
	}
	inv.replace(i, obj)
	return inv.save()
}

// GET /api/nodes/{id}/revoke-key: what a revoke would remove; never contacts the node.
func handleRevokeKeyPreview(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if err := validateNodeID(id); err != nil {
		writeAPIError(w, err)
		return
	}
	node, err := findNode(id)
	if err != nil {
		writeAPIError(w, err)
		return
	}
	if isHubNode(node) {
		writeAPIError(w, errRevokeHub)
		return
	}
	match, fp, err := hubKeyMatch()
	if err != nil {
		writeAPIError(w, err)
		return
	}
	jsonResponse(w, http.StatusOK, map[string]any{
		"node": id, "target": nodeTarget(node), "fingerprint": fp, "match": match,
		"note": "POST with {\"confirm_id\": \"" + id + "\"} removes every line of ~/.ssh/authorized_keys on the node containing this key. " + revokeNote,
	})
}

// @MX:WARN: [AUTO] Edits ~/.ssh/authorized_keys on a remote node over the fleet key.
// @MX:REASON: Removes the hub's own access; the script is fixed, the key is validated base64, a timestamped backup is kept.
func handleRevokeKey(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	a := beginAudit(r, "/api/nodes/{id}/revoke-key", id)
	defer a.done()
	if err := validateNodeID(id); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	var req struct {
		ConfirmID string `json:"confirm_id"`
	}
	if err := decodeNodeBody(w, r, &req); err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	if req.ConfirmID != id {
		a.fail(errors.New("confirm_id must equal the node id"))
		writeAPIError(w, &apiError{Status: http.StatusBadRequest, Code: "confirm_mismatch", Field: "confirm_id", Msg: "confirm_id must equal the node id"})
		return
	}
	node, err := findNode(id)
	if err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	if isHubNode(node) {
		a.fail(errRevokeHub)
		writeAPIError(w, errRevokeHub)
		return
	}
	match, _, err := hubKeyMatch()
	if err != nil {
		a.fail(err)
		writeAPIError(w, err)
		return
	}
	unlock, lerr := lockFleet()
	if lerr != nil {
		a.fail(lerr)
		writeAPIError(w, lerr)
		return
	}
	defer unlock()

	code, stdout, stderr, _ := sshProbe(id, fmt.Sprintf(revokeScript, match), revokeTimeoutSec)
	m := removedRe.FindStringSubmatch(stdout)
	if code != 0 || m == nil {
		reach := classifySSH(code, stderr)
		a.fail(fmt.Errorf("revoke failed (%s)", reach))
		jsonResponse(w, http.StatusBadGateway, map[string]any{
			"error":        "could not revoke the hub key on " + id + ": " + tailText(strings.TrimSpace(stderr), probeStderrLimit),
			"reachability": reach,
		})
		return
	}
	n, _ := strconv.Atoi(m[1])
	resp := map[string]any{"ok": true, "removed": n}
	now := time.Now().UTC()
	if merr := markHubKeyRevoked(id, now); merr != nil {
		resp["warning"] = "the key was removed but hub_key_revoked_at could not be recorded: " + merr.Error()
	} else {
		resp["hub_key_revoked_at"] = now.Format(time.RFC3339)
	}
	a.ok("revoke hub key: removed=" + strconv.Itoa(n))
	if n == 0 {
		resp["note"] = "no line containing the hub key was found in ~/.ssh/authorized_keys; nothing was changed. " + revokeNote
	} else {
		resp["note"] = revokeNote
	}
	jsonResponse(w, http.StatusOK, resp)
}
