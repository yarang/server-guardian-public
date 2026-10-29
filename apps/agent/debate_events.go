package main

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"syscall"
	"time"
)

// Debate event stream (docs/design/debate-agent-retrieval.md): the Python CLI appends one JSON
// object per line to <job dir>/events.jsonl; the hub only relays those lines.
const (
	eventsFileName         = "events.jsonl"
	promptsDirName         = "prompts" // <job dir>/prompts/<seq>.txt: the turn prompt of an agent_started event
	maxPromptFile          = 4 << 20   // the CLI caps each prompt at 2 MiB; anything larger is not served
	maxEventLine           = 1 << 20   // longer lines are skipped
	maxEventsPerCall       = 1000
	eventPollInterval      = 300 * time.Millisecond
	eventHeartbeatInterval = 15 * time.Second
)

var eventAfterForm = regexp.MustCompile(`^[0-9]{1,15}$`)

func (h *debateHub) eventsPath(id string) string {
	return filepath.Join(h.jobsDir(), id, eventsFileName)
}

// debatePrompt serves the prompt saved for one agent_started event (data.prompt_saved). The CLI
// writes the file before the event line, so a client that saw the event never gets a 404 for it.
func (h *debateHub) debatePrompt(w http.ResponseWriter, r *http.Request) {
	id, rawSeq := r.PathValue("id"), r.PathValue("seq")
	if !jobIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	if !eventAfterForm.MatchString(rawSeq) {
		errJSON(w, http.StatusBadRequest, "invalid seq")
		return
	}
	seq, _ := strconv.ParseInt(rawSeq, 10, 64)
	if _, err := h.readMeta(id); err != nil {
		errJSON(w, http.StatusNotFound, "debate not found")
		return
	}
	path := filepath.Join(h.jobsDir(), id, promptsDirName, strconv.FormatInt(seq, 10)+".txt")
	// O_NOFOLLOW + regular file check: never a link out of the job directory.
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		errJSON(w, http.StatusNotFound, "prompt not found")
		return
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() {
		errJSON(w, http.StatusNotFound, "prompt not found")
		return
	}
	if st.Size() > maxPromptFile {
		errJSON(w, http.StatusRequestEntityTooLarge, "prompt too large")
		return
	}
	data, err := io.ReadAll(io.LimitReader(f, maxPromptFile))
	if err != nil {
		errJSON(w, http.StatusInternalServerError, "cannot read prompt")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, map[string]interface{}{"seq": seq, "bytes": len(data), "text": string(data)})
}

func (h *debateHub) hasEvents(id string) bool {
	st, err := os.Lstat(h.eventsPath(id))
	return err == nil && st.Mode().IsRegular()
}

// scanEvents reads complete lines of the events file starting at offset and calls visit with
// the seq and raw bytes of every valid event object. Lines that are not JSON objects, carry no
// numeric seq, or exceed maxEventLine are skipped. A trailing line without a newline is still
// being written: it is not consumed, so the returned offset points at its start. visit returns
// false to stop early; the offset then points after the last line it accepted.
func scanEvents(path string, offset int64, visit func(seq int64, raw []byte) bool) (int64, error) {
	// O_NOFOLLOW: the file must be the job's own regular file, never a link out of the directory.
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		if os.IsNotExist(err) {
			return offset, nil
		}
		return offset, err
	}
	defer f.Close()
	if offset > 0 {
		if _, err := f.Seek(offset, 0); err != nil {
			return offset, err
		}
	}
	br := bufio.NewReaderSize(f, 64<<10)
	for {
		var line []byte
		over := false
		consumed := 0
		for {
			chunk, rerr := br.ReadSlice('\n')
			consumed += len(chunk)
			if !over {
				line = append(line, chunk...)
				if len(line) > maxEventLine+1 {
					over, line = true, nil // keep counting bytes but stop buffering
				}
			}
			if rerr == bufio.ErrBufferFull {
				continue
			}
			if rerr != nil {
				return offset, nil // EOF: no further complete line (or a partial one to retry)
			}
			break
		}
		offset += int64(consumed)
		if over {
			continue
		}
		line = line[:len(line)-1] // drop '\n'
		if n := len(line); n > 0 && line[n-1] == '\r' {
			line = line[:n-1]
		}
		var probe struct {
			Seq *int64 `json:"seq"`
		}
		if len(line) == 0 || line[0] != '{' || json.Unmarshal(line, &probe) != nil || probe.Seq == nil {
			continue
		}
		if !visit(*probe.Seq, line) {
			return offset, nil
		}
	}
}

// jobFinished reports whether the job is no longer running and its stored status.
func (h *debateHub) jobFinished(id string) (bool, string) {
	sum, err := h.readMeta(id)
	if err != nil {
		return true, "unknown"
	}
	return sum.Status != "running", sum.Status
}

func (h *debateHub) debateEvents(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !jobIDPattern.MatchString(id) {
		errJSON(w, http.StatusBadRequest, "invalid id")
		return
	}
	q := r.URL.Query()
	var after int64
	if raw := q.Get("after"); raw != "" {
		if !eventAfterForm.MatchString(raw) {
			errJSON(w, http.StatusBadRequest, "invalid after")
			return
		}
		after, _ = strconv.ParseInt(raw, 10, 64)
	}
	follow := false
	switch q.Get("follow") {
	case "", "0", "false":
	case "1", "true":
		follow = true
	default:
		errJSON(w, http.StatusBadRequest, "invalid follow")
		return
	}
	if _, err := h.readMeta(id); err != nil {
		errJSON(w, http.StatusNotFound, "debate not found")
		return
	}
	if follow {
		h.followEvents(w, r, id, after)
		return
	}

	// Read the state before the file: if the job was finished first, every line is on disk.
	finished, _ := h.jobFinished(id)
	events := make([]json.RawMessage, 0, 64)
	next := after
	more := false
	_, err := scanEvents(h.eventsPath(id), 0, func(seq int64, raw []byte) bool {
		if seq <= after {
			return true
		}
		if len(events) >= maxEventsPerCall {
			more = true
			return false
		}
		events = append(events, append(json.RawMessage(nil), raw...))
		next = seq
		return true
	})
	if err != nil {
		errJSON(w, http.StatusInternalServerError, "cannot read events")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	jsonResponse(w, http.StatusOK, map[string]interface{}{"events": events, "next": next, "done": finished && !more})
}

// followEvents streams events as NDJSON: lines with seq > after, then new lines as they are
// appended, a heartbeat while idle, and a final end message once the job has finished.
func (h *debateHub) followEvents(w http.ResponseWriter, r *http.Request, id string, after int64) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		errJSON(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	hdr := w.Header()
	hdr.Set("Content-Type", "application/x-ndjson")
	hdr.Set("Cache-Control", "no-store")
	hdr.Set("X-Accel-Buffering", "no") // keep reverse proxies from buffering the stream
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	ctx, cancel := context.WithTimeout(r.Context(), h.cfg.JobTimeout)
	defer cancel()
	poll := time.NewTicker(h.eventPoll)
	defer poll.Stop()
	lastWrite := time.Now()
	var offset int64
	send := func(line []byte) bool {
		if _, err := w.Write(append(append([]byte(nil), line...), '\n')); err != nil {
			return false
		}
		flusher.Flush()
		lastWrite = time.Now()
		return true
	}
	for {
		// State first, then drain: a job seen as finished has no lines left unread afterwards.
		finished, status := h.jobFinished(id)
		wrote := true
		var err error
		offset, err = scanEvents(h.eventsPath(id), offset, func(seq int64, raw []byte) bool {
			if seq <= after {
				return true
			}
			wrote = send(raw)
			return wrote
		})
		if err != nil || !wrote {
			return
		}
		if finished {
			end, _ := json.Marshal(struct {
				Type   string `json:"type"`
				Status string `json:"status"`
			}{"end", status})
			send(end)
			return
		}
		if time.Since(lastWrite) >= h.eventHeartbeat && !send([]byte(`{"type":"heartbeat"}`)) {
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-poll.C:
		}
	}
}
