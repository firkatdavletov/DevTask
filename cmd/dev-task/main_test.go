package main

import (
	"bytes"
	"testing"
)

func TestRun(t *testing.T) {
	tests := []struct {
		name string
		args []string
		code int
		want string
	}{
		{"missing command", nil, 2, "dev-task: a command is required\n"},
		{"command unavailable", []string{"run"}, 1, "dev-task: Go preview does not implement commands yet\n"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var stderr bytes.Buffer
			if got := run(tt.args, &stderr); got != tt.code {
				t.Errorf("exit code = %d, want %d", got, tt.code)
			}
			if got := stderr.String(); got != tt.want {
				t.Errorf("stderr = %q, want %q", got, tt.want)
			}
		})
	}
}
