#!/usr/bin/env python3
"""Check every manifest in openshift-ai-steps.md is still copy-pasteable.

Each step of the walkthrough writes a manifest with

    cat > "$M/<name>.yaml" <<EOF
    ...
    EOF
    oc apply -f "$M/<name>.yaml"

and reading the markdown proves nothing about what `cat` actually writes: with
an unquoted heredoc the shell expands every `$`, backtick and backslash in the
YAML first. Step 6's ServingRuntime is a shell script, so it needs <<'EOF' and
loses whole lines without it.

So: pull each block out, run it in a real shell, and check that what lands on
disk is valid YAML and byte-identical to what the document shows.

    python3 tests/check-steps-manifests.py        # needs PyYAML, no cluster

Exits non-zero on the first block that does not survive. Run it after editing
any manifest in the walkthrough.
"""
import pathlib, re, subprocess, sys, tempfile, yaml

HERE = pathlib.Path(__file__).resolve().parent.parent
md = (HERE / 'openshift-ai-steps.md').read_text()

# every bash block that writes a manifest and applies it
blocks = re.findall(r'```bash\n(cat > "\$M/[^\n]+\n.*?\nEOF\noc apply -f "\$M/[^\n]+"\n)```',
                    md, re.S)
if not blocks:
    sys.exit("no write-and-apply blocks found - has the format changed?")
print(f"found {len(blocks)} write-and-apply blocks in openshift-ai-steps.md\n")
fail = 0
with tempfile.TemporaryDirectory() as d:
    for b in blocks:
        fname = re.search(r'cat > "\$M/([^"]+)"', b).group(1)
        quoted = "<<'EOF'" in b.splitlines()[0]
        # run only the cat part; oc is not here
        cat_part = b.split('\nEOF\n')[0] + '\nEOF\n'
        r = subprocess.run(['bash', '-c', f'set -eu\nexport M={d}\n{cat_part}'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  {fname:28} SHELL FAILED: {r.stderr.strip()[:80]}"); fail = 1; continue
        written = pathlib.Path(d, fname).read_text()
        # 1. is it valid YAML?
        try:
            docs = [x for x in yaml.safe_load_all(written) if x]
        except Exception as e:
            print(f"  {fname:28} INVALID YAML: {e}"); fail = 1; continue
        # 2. does it match the document byte for byte? (the expansion check)
        shown = b.split('\n', 1)[1].rsplit('\nEOF\n', 1)[0] + '\n'
        same = written == shown
        kinds = ", ".join(f"{x.get('kind')}/{x.get('metadata',{}).get('name')}" for x in docs)
        flag = "<<'EOF'" if quoted else "<<EOF  "
        print(f"  {fname:28} {flag} {len(docs)} doc(s)  "
              f"{'unchanged' if same else 'DIFFERS FROM DOC'}  {kinds[:52]}")
        if not same:
            fail = 1
            for a, bl in zip(shown.splitlines(), written.splitlines()):
                if a != bl:
                    print(f"      doc:  {a!r}\n      disk: {bl!r}")
                    break
sys.exit(fail)
