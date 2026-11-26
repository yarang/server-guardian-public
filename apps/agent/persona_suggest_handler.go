package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

// AI suggestion of a persona and profile (docs/design/agent-characters.md, section 2.6): one LLM turn of the CLI,
// nothing saved. One suggestion runs at a time; a second request gets 409 instead of queueing behind a model turn.

const (
	personaSuggestTimeout = 5 * time.Minute
	// personaSuggestFailed is the CLI's exit code when the model gave no usable draft.
	personaSuggestFailed = 5
)

var personaSuggestSlot = make(chan struct{}, 1)

func (h *debateHub) personaSuggest(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, configBodyLimit))
	if err != nil {
		var mbe *http.MaxBytesError
		if errors.As(err, &mbe) {
			errJSON(w, http.StatusRequestEntityTooLarge, "request body too large")
			return
		}
		errJSON(w, http.StatusBadRequest, "could not read request body")
		return
	}
	select {
	case personaSuggestSlot <- struct{}{}:
		defer func() { <-personaSuggestSlot }()
	default:
		errJSON(w, http.StatusConflict, "another persona suggestion is running")
		return
	}
	code, out, errOut, err := h.runCLI(personaSuggestTimeout, body, "--persona-suggest")
	if err == nil && code == personaSuggestFailed {
		var res struct {
			Error string `json:"error"`
		}
		msg := "the model gave no usable draft"
		if json.Unmarshal(out, &res) == nil && res.Error != "" {
			msg = res.Error
		} else if tail := tailString(strings.TrimSpace(errOut), errLogTailBytes); tail != "" {
			msg = joinErr(msg, tail)
		}
		errJSON(w, http.StatusBadGateway, msg)
		return
	}
	h.respondConfig(w, code, out, errOut, err)
}
