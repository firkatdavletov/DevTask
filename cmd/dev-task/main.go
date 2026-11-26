package main

import (
	"fmt"
	"io"
	"os"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stderr))
}

func run(args []string, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "dev-task: a command is required")
		return 2
	}

	fmt.Fprintln(stderr, "dev-task: Go preview does not implement commands yet")
	return 1
}
