package main

import (
	"bytes"
	"fmt"
)

// maxCommandOutput bounds what the hub keeps of one command's stdout or stderr. A command may run for an hour and an
// approved script can print without end; the Python side keeps only the last few thousand characters anyway.
const maxCommandOutput = 1 << 20

// cappedBuffer keeps the first limit bytes it is given and counts the rest. Write never fails, so the command is not
// killed by a broken pipe; it only loses output nobody reads.
type cappedBuffer struct {
	buf     bytes.Buffer
	limit   int
	dropped int64
}

func newCappedBuffer(limit int) *cappedBuffer {
	return &cappedBuffer{limit: limit}
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	room := c.limit - c.buf.Len()
	if room > len(p) {
		room = len(p)
	}
	if room > 0 {
		c.buf.Write(p[:room])
	}
	c.dropped += int64(len(p) - max(room, 0))
	return len(p), nil
}

func (c *cappedBuffer) String() string {
	if c.dropped == 0 {
		return c.buf.String()
	}
	return c.buf.String() + fmt.Sprintf("\n[output truncated: %d more bytes dropped]", c.dropped)
}
