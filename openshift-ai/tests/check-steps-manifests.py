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

# Every block that writes a manifest. The `oc apply` may be in the same block,
# or - where the reader has to edit a value first, so pasting both at once
# would apply the placeholder - in a block of its own further down. Both
# shapes are accepted; what is checked is that an apply for the file exists
# somewhere after it.
blocks = re.findall(r'```bash\n(cat > "\$M/[^\n]+\n.*?\nEOF\n)', md, re.S)
if not blocks:
    sys.exit("no manifest blocks found - has the format changed?")
print(f"found {len(blocks)} manifest blocks in openshift-ai-steps.md\n")
fail = 0
with tempfile.TemporaryDirectory() as d:
    for b in blocks:
        fname = re.search(r'cat > "\$M/([^"]+)"', b).group(1)
        quoted = "<<'EOF'" in b.splitlines()[0]
        # is it ever applied? a manifest the walkthrough writes and never
        # applies is a step the reader cannot finish
        where = md.index(b)
        applied = f'oc apply -f "$M/{fname}"' in md[where:]
        # oc is not here, so run only the cat
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
        body_shown = shown
        same = written == shown
        kinds = ", ".join(f"{x.get('kind')}/{x.get('metadata',{}).get('name')}" for x in docs)
        flag = "<<'EOF'" if quoted else "<<EOF  "
        # Is the apply in the same block, or split into one of its own? Split
        # is deliberate wherever the reader has to edit a value first.
        after = md[where + len(b):]
        shape = "cat+apply" if after.startswith(f'oc apply -f "$M/{fname}"') else "apply split out"
        print(f"  {fname:28} {flag} {len(docs)} doc(s)  "
              f"{'unchanged' if same else 'DIFFERS FROM DOC'}  "
              f"{'applied' if applied else 'NEVER APPLIED'}  {shape:15} {kinds[:34]}")
        if not applied:
            fail = 1
        # A manifest carrying a value the reader must take from an earlier
        # step ("# <-") must have its apply in a block of its own: pasting
        # cat-and-apply together would apply the placeholder. And one with
        # nothing to edit should stay a single paste.
        edits = sum(1 for l in body_shown.splitlines() if '# <-' in l)
        if (edits > 0) != (shape == "apply split out"):
            print(f"      WRONG SHAPE: {edits} value(s) marked '# <-' but the apply is "
                  f"{'in the same block' if shape == 'cat+apply' else 'split out'}")
            fail = 1
        if not same:
            fail = 1
            for a, bl in zip(shown.splitlines(), written.splitlines()):
                if a != bl:
                    print(f"      doc:  {a!r}\n      disk: {bl!r}")
                    break
sys.exit(fail)
