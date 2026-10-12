#!/usr/bin/env python3
"""Check every shell block in this directory's documentation parses.

Not that the commands are right - nothing here talks to a cluster - but that
they are syntactically valid bash, so a reader can paste one without the shell
refusing it.

The failure this catches is unobvious: an angle-bracket placeholder is a
REDIRECT to bash, so

    oc delete validatingwebhookconfiguration <name>

is a syntax error rather than a prompt to substitute something, and
`oc adm uncordon <node>` silently reads a file called "node". A placeholder
that has to stay is fine as long as it is quoted - "<name>" - or in prose
rather than in a ```bash block.

    python3 tests/check-doc-shell.py        # needs bash, no cluster

Exits non-zero, naming the block and the bash error.
"""
import pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent.parent
DOCS = ('openshift-ai-steps.md', 'troubleshooting.md', 'README.md')

fail = 0
for name in DOCS:
    f = HERE / name
    if not f.exists():
        sys.exit(f"{name}: not found - has it been renamed?")
    blocks = re.findall(r'```bash\n(.*?)\n```', f.read_text(), re.S)
    bad = []
    for i, b in enumerate(blocks, 1):
        r = subprocess.run(['bash', '-n'], input=b, capture_output=True, text=True)
        if r.returncode != 0:
            err = (r.stderr.strip().splitlines() or ['(no message)'])[0]
            bad.append((i, b.splitlines()[0][:60], err[:80]))
    print(f"  {name:26} {len(blocks):3} bash blocks, {len(bad)} with syntax errors")
    for i, first, err in bad:
        print(f"      block {i}: {first}\n        {err}")
        fail = 1
sys.exit(fail)
