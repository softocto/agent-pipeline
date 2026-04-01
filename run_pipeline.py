"""
5-Agent Coding Pipeline - Global CLI
======================================
Coder agent uses tools to edit files directly (like Claude Code).
Now with context-returning edits and auto-reread on failure.

Commands:
  agent-pipeline "idea"              Full review (all 5 agents)
  agent-pipeline --apply "idea"      Fast apply: Architect + Coder (tools) + Supervisor
  agent-pipeline --merge             Security + Tester review, then merge
  agent-pipeline --rollback          Discard feature branch
"""

import os, sys, re, json, time, subprocess
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
import anthropic

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
CODER_MAX_TOKENS = 16384
PROMPTS_DIR = SCRIPT_DIR / "prompts"

IGNORE_DIRS = {
    "node_modules",".next","dist","build",".cache","coverage",".nyc_output",
    ".turbo",".vercel",".output","storybook-static","out",".docusaurus",".expo",
    ".venv","venv","env","__pycache__",".mypy_cache",".pytest_cache",".ruff_cache",
    "htmlcov",".tox","site-packages",".eggs",".git",".agent-pipeline","outputs","data","logs","tmp","temp","scripts",
}
IGNORE_FILES = {"package-lock.json","yarn.lock","pnpm-lock.yaml","poetry.lock","Pipfile.lock",".DS_Store","Thumbs.db"}
CONFIG_FILES = {
    "package.json","tsconfig.json","vite.config.ts","vite.config.js",
    "next.config.js","next.config.ts","next.config.mjs","tailwind.config.js","tailwind.config.ts",
    "requirements.txt","pyproject.toml","setup.py","setup.cfg",
    "Pipfile","manage.py","alembic.ini","config/settings.py",
    ".env.example","prisma/schema.prisma","docker-compose.yml","Dockerfile","README.md",
}
SOURCE_EXTENSIONS = {".py",".js",".jsx",".ts",".tsx",".html",".htm",".css",".scss",".vue",".svelte",}

MAX_CONFIG_LINES = 80
MAX_SOURCE_LINES = 800
MAX_TOTAL_SOURCE_LINES = 5000
MAX_SOURCE_FILE_SIZE = 50000
BRANCH_INFO_FILE = ".agent-pipeline/.branch-info"

# ---------------------------------------------------------------------------
# Git Helpers
# ---------------------------------------------------------------------------
def git_run(d,*a):
    try:
        r=subprocess.run(["git"]+list(a),cwd=d,capture_output=True,
            encoding="utf-8",errors="replace",timeout=60)
        return r.returncode==0,(r.stdout or "").strip()
    except FileNotFoundError: return False,"Git not installed"
    except subprocess.TimeoutExpired: return False,"Timeout"

def git_is_repo(d): ok,_=git_run(d,"rev-parse","--is-inside-work-tree"); return ok
def git_init_repo(d):
    print("  Initializing git repo...")
    git_run(d,"init"); git_run(d,"add","-A"); git_run(d,"commit","-m","Initial commit")
def git_has_changes(d): ok,o=git_run(d,"status","--porcelain"); return bool(o.strip())
def git_current_branch(d): ok,b=git_run(d,"rev-parse","--abbrev-ref","HEAD"); return b if ok else "main"
def git_create_branch(d,n): ok,_=git_run(d,"checkout","-b",n); return ok
def git_commit(d,m): git_run(d,"add","-A"); ok,_=git_run(d,"commit","-m",m); return ok
def git_checkout(d,b): ok,_=git_run(d,"checkout",b); return ok
def git_merge(d,b): ok,o=git_run(d,"merge",b); return ok,o
def git_delete_branch(d,b): ok,_=git_run(d,"branch","-D",b); return ok
def git_diff_summary(d,base,feat): ok,o=git_run(d,"diff","--stat",f"{base}...{feat}"); return o if ok else ""
def git_diff_full(d,base,feat): ok,o=git_run(d,"diff",f"{base}...{feat}"); return o if ok else ""
def is_feature_branch(b): return b.startswith("feature/")
def make_branch_name(idea):
    s=re.sub(r'\s+','-',re.sub(r'[^a-zA-Z0-9\s]','',idea[:50]).strip()).lower()
    return f"feature/{s}-{datetime.now().strftime('%m%d%H%M')}"
def save_branch_info(d,orig,feat):
    p=d/BRANCH_INFO_FILE; p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps({"original_branch":orig,"feature_branch":feat}),encoding="utf-8")
def load_branch_info(d):
    p=d/BRANCH_INFO_FILE
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except: pass
    return None

# ---------------------------------------------------------------------------
# Merge & Rollback
# ---------------------------------------------------------------------------
def do_merge(d, max_fix_rounds=3):
    from concurrent.futures import ThreadPoolExecutor

    info=load_branch_info(d); cur=git_current_branch(d)
    if info: orig,feat=info["original_branch"],info["feature_branch"]
    elif is_feature_branch(cur): feat,orig=cur,"main"
    else: print("  Not on a feature branch."); return
    if cur!=feat: git_checkout(d,feat)
    if git_has_changes(d): git_commit(d,"WIP: before merge")
    diff=git_diff_summary(d,orig,feat)
    if not diff: print("  No changes."); return
    print(f"\n  Changes on '{feat}':\n")
    for l in diff.splitlines(): print(f"    {l}")

    ctx_full=scan_project(d)
    ctx_light=scan_project_light(d)
    client=anthropic.Anthropic()
    rd=d/".agent-pipeline"/"merge_review"; rd.mkdir(parents=True,exist_ok=True)

    for fix_round in range(max_fix_rounds + 1):
        if fix_round > 0:
            print(f"\n  {'='*55}\n  FIX ROUND {fix_round} OF {max_fix_rounds}\n  {'='*55}")

        # 1. Tester + Security in parallel
        full_diff=git_diff_full(d,orig,feat)
        print(f"\n  Running security & test review (parallel)...")
        review_msg=f"## PROJECT\n{ctx_full}\n\n## CHANGES\n```diff\n{full_diff[:15000]}\n```\n\nReview."
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_tester=pool.submit(call_agent,client,"tester",load_prompt("tester"),review_msg)
            fut_security=pool.submit(call_agent,client,"security",load_prompt("security"),review_msg)
            tester_result=fut_tester.result()
            security_result=fut_security.result()
        (rd/f"tester_round{fix_round}.md").write_text(tester_result,encoding="utf-8")
        (rd/f"security_round{fix_round}.md").write_text(security_result,encoding="utf-8")

        # 2. Supervisor reviews everything
        sup_msg=(f"## EXISTING PROJECT\n{ctx_light}\n\n---\n\n"
            f"## CHANGES\n```diff\n{full_diff[:15000]}\n```\n\n---\n\n"
            f"## TESTER REPORT\n\n{tester_result}\n\n---\n\n"
            f"## SECURITY REPORT\n\n{security_result}\n\n---\n\n"
            f"Pre-merge review. This code must be bug-free and secure before merging.\n"
            f"If there are ANY bugs or security vulnerabilities, say NEEDS REVISION and list each issue precisely.\n"
            f"Only say APPROVED if the code is ready to merge with zero known bugs and no exploitable vulnerabilities.\n"
            f"Verdict.")
        sup=call_agent(client,"supervisor",load_prompt("supervisor"),sup_msg)
        (rd/f"supervisor_round{fix_round}.md").write_text(sup,encoding="utf-8")

        approved="APPROVED" in sup.upper()
        needs_revision="NEEDS REVISION" in sup.upper()

        if approved:
            print(f"\n  Supervisor: APPROVED")
            break

        if not needs_revision or fix_round >= max_fix_rounds:
            print(f"\n  Supervisor: NOT APPROVED after {fix_round + 1} round(s).")
            print(f"  Review the findings in: {rd}")
            if input(f"\n  Merge anyway? (y/n): ").strip().lower()!="y": return
            break

        # 3. Coder fixes the issues
        print(f"\n  Supervisor: NEEDS REVISION - sending to coder for fixes...")
        target_files = extract_target_files(sup, d)
        if not target_files:
            target_files = extract_target_files(full_diff, d)
        preloaded = preload_files(d, target_files)

        fix_instructions=(f"## SUPERVISOR FEEDBACK\n\n{sup}\n\n---\n\n"
            f"## TESTER FINDINGS\n\n{tester_result}\n\n---\n\n"
            f"## SECURITY FINDINGS\n\n{security_result}\n\n---\n\n"
            f"Fix ALL bugs and security issues listed above. Every issue must be resolved.")
        coder_summary, modified, created = run_coder_with_tools(client, d, fix_instructions, ctx_light, preloaded)
        (rd/f"coder_fix_round{fix_round}.md").write_text(coder_summary,encoding="utf-8")

        if modified or created:
            git_commit(d,f"fix: address review findings (round {fix_round + 1})")
            print(f"  Fixes committed.")
        else:
            print(f"  Coder made no changes. Stopping fix loop.")
            if input(f"\n  Merge anyway? (y/n): ").strip().lower()!="y": return
            break

    # Merge
    if input(f"\n  Merge '{feat}' into '{orig}'? (y/n): ").strip().lower()!="y": return
    git_checkout(d,orig); ok,_=git_merge(d,feat)
    if ok:
        print(f"\n  Merged into '{orig}'.")
        if input(f"  Delete '{feat}'? (y/n): ").strip().lower()=="y": git_delete_branch(d,feat)
        p=d/BRANCH_INFO_FILE
        if p.exists(): p.unlink()
    else: print("  Conflict. Resolve, then: git add . && git commit")

def do_rollback(d):
    info=load_branch_info(d); cur=git_current_branch(d)
    if info: orig,feat=info["original_branch"],info["feature_branch"]
    elif is_feature_branch(cur): feat,orig=cur,"main"
    else: print("  Nothing to roll back."); return
    print(f"  This will DELETE '{feat}' and all changes.")
    if input("  Sure? (y/n): ").strip().lower()!="y": return
    git_checkout(d,orig); git_delete_branch(d,feat)
    p=d/BRANCH_INFO_FILE
    if p.exists(): p.unlink()
    print(f"\n  Rolled back to '{orig}'.")

# ---------------------------------------------------------------------------
# Project Scanner
# ---------------------------------------------------------------------------
def scan_file_tree(project_dir):
    """Scan just the file tree structure (no file contents)."""
    parts=[]; fc=0; all_src=[]
    parts.append(f"## Project File Structure\nProject root: {project_dir.name}/ (use paths relative to root, e.g. 'index.html' not '{project_dir.name}/index.html')\n```")
    for root,dirs,files in os.walk(project_dir):
        dirs[:]=sorted([d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")])
        depth=len(Path(root).relative_to(project_dir).parts)
        if depth>4: dirs.clear(); continue
        indent="  "*depth
        parts.append(f"{indent}{Path(root).name if depth>0 else '.'}/")
        for f in sorted(files):
            if f not in IGNORE_FILES and not f.startswith("."):
                parts.append(f"{indent}  {f}"); fc+=1
                fp=Path(root)/f
                if fp.suffix.lower() in SOURCE_EXTENSIONS: all_src.append(fp)
        if fc>200: parts.append("  ..."); break
    parts.append("```\n")
    return "\n".join(parts), all_src

def scan_config_files(project_dir):
    """Scan configuration files only."""
    parts=[]
    parts.append("## Key Configuration Files\n")
    for fn in CONFIG_FILES:
        fp=project_dir/fn
        if fp.exists():
            try:
                lines=fp.read_text(encoding="utf-8").splitlines()
                c="\n".join(lines[:MAX_CONFIG_LINES])
                if len(lines)>MAX_CONFIG_LINES: c+=f"\n... ({len(lines)} lines)"
                parts.append(f"### {fn}\n```\n{c}\n```\n")
            except: pass
    return "\n".join(parts)

def scan_tech_stack(project_dir):
    """Detect tech stack from config files."""
    stack=[]
    pp=project_dir/"package.json"
    if pp.exists():
        try:
            pkg=json.loads(pp.read_text(encoding="utf-8"))
            deps={**pkg.get("dependencies",{}),**pkg.get("devDependencies",{})}
            for k,v in {"react":"React","next":"Next.js","typescript":"TypeScript","tailwindcss":"Tailwind","express":"Express"}.items():
                if k in deps: stack.append(f"{v} {deps[k]}" if deps[k] else v)
        except: pass
    rp=project_dir/"requirements.txt"
    if rp.exists():
        try:
            pkgs=[l.split("==")[0].split(">=")[0].strip().lower() for l in rp.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#") and not l.startswith("-")]
            if pkgs:
                stack.append("Python")
                for k,v in {"django":"Django","flask":"Flask","fastapi":"FastAPI","sqlalchemy":"SQLAlchemy","pytest":"pytest"}.items():
                    if k in pkgs: stack.append(v)
        except: pass
    if stack: return f"## Detected Tech Stack\n{', '.join(stack)}\n"
    return ""

def scan_source_files(project_dir, all_src):
    """Scan source file contents (expensive — used for architect context)."""
    parts=[]
    parts.append("## Source Files\n")
    tl=0;fr=0;fs=[];swz=[]
    lm={".py":"python",".js":"javascript",".jsx":"jsx",".ts":"typescript",".tsx":"tsx",
        ".html":"html",".htm":"html",".css":"css",".scss":"scss",".json":"json"}
    for fp in all_src:
        try:
            sz=fp.stat().st_size
            if sz<=MAX_SOURCE_FILE_SIZE: swz.append((fp,sz))
            else: fs.append(str(fp.relative_to(project_dir)))
        except: pass
    swz.sort(key=lambda x:x[1])
    for fp,sz in swz:
        if tl>=MAX_TOTAL_SOURCE_LINES: fs.append(str(fp.relative_to(project_dir))); continue
        try: lines=fp.read_text(encoding="utf-8").splitlines()
        except: continue
        rel=fp.relative_to(project_dir)
        if len(lines)>MAX_SOURCE_LINES:
            c="\n".join(lines[:MAX_SOURCE_LINES])+f"\n... (truncated, {len(lines)} lines)"; tl+=MAX_SOURCE_LINES
        else: c="\n".join(lines); tl+=len(lines)
        parts.append(f"### {rel}\n```{lm.get(fp.suffix.lower(),'')}\n{c}\n```\n"); fr+=1
    if fs: parts.append(f"**Not shown:** {', '.join(fs[:15])}\n")
    parts.append(f"*{fr} files, {tl} lines.*\n")
    return "\n".join(parts)

def scan_project(project_dir):
    """Full scan: tree + config + source + tech stack (for architect)."""
    tree, all_src = scan_file_tree(project_dir)
    config = scan_config_files(project_dir)
    source = scan_source_files(project_dir, all_src)
    stack = scan_tech_stack(project_dir)
    return "\n".join([tree, config, source, stack])

def scan_project_light(project_dir):
    """Light scan: tree + config + tech stack only (no source contents)."""
    tree, _ = scan_file_tree(project_dir)
    config = scan_config_files(project_dir)
    stack = scan_tech_stack(project_dir)
    return "\n".join([tree, config, stack])

# ---------------------------------------------------------------------------
# Coder Tools — now returns context after edits
# ---------------------------------------------------------------------------
CODER_TOOLS = [
    {"name":"read_file",
     "description":"Read a file's contents. ALWAYS call this before your first edit to a file.",
     "input_schema":{"type":"object","properties":{"path":{"type":"string","description":"File path relative to project root"}},"required":["path"]}},
    {"name":"str_replace",
     "description":"Replace an exact string in a file. old_str must match the CURRENT file exactly (after any previous edits). Returns the edited area so you can see the result. Make one small change at a time.",
     "input_schema":{"type":"object","properties":{
         "path":{"type":"string","description":"File path"},
         "old_str":{"type":"string","description":"Exact existing text to find (must appear once). Copy from most recent read_file or from the context returned by the previous edit."},
         "new_str":{"type":"string","description":"Replacement text"}},"required":["path","old_str","new_str"]}},
    {"name":"create_file",
     "description":"Create a new file (must not already exist).",
     "input_schema":{"type":"object","properties":{
         "path":{"type":"string","description":"File path"},
         "content":{"type":"string","description":"File content"}},"required":["path","content"]}},
    {"name":"insert_at",
     "description":"Insert new content after an exact anchor string in a file. Good for adding new blocks without replacing anything.",
     "input_schema":{"type":"object","properties":{
         "path":{"type":"string","description":"File path"},
         "after":{"type":"string","description":"Exact anchor string to insert after (must appear once)"},
         "content":{"type":"string","description":"New content to insert"}},"required":["path","after","content"]}},
    {"name":"write_file",
     "description":"Overwrite an entire existing file with new content. Use this instead of multiple str_replace calls when you need to make many changes to a single file. You MUST read_file first to see the current content. Then provide the complete updated file.",
     "input_schema":{"type":"object","properties":{
         "path":{"type":"string","description":"File path (must already exist; use create_file for new files)"},
         "content":{"type":"string","description":"Complete new file content (replaces everything)"}},"required":["path","content"]}},
]

def get_surrounding_context(content, position, context_lines=10):
    """Get lines surrounding a position in the file."""
    lines = content.splitlines()
    # Find which line the position falls on
    char_count = 0
    target_line = 0
    for i, line in enumerate(lines):
        char_count += len(line) + 1  # +1 for newline
        if char_count >= position:
            target_line = i
            break
    start = max(0, target_line - context_lines)
    end = min(len(lines), target_line + context_lines + 1)
    context = "\n".join(f"  {start+j+1}: {lines[start+j]}" for j in range(end - start))
    return f"[Lines {start+1}-{end} of {len(lines)}]\n{context}"


def execute_tool(tool_name, tool_input, project_dir):
    path = tool_input.get("path", "")
    filepath = project_dir / path
    try: filepath.resolve().relative_to(project_dir.resolve())
    except ValueError: return f"ERROR: '{path}' is outside project."

    if tool_name == "read_file":
        if not filepath.exists(): return f"ERROR: Not found: {path}"
        try:
            content = filepath.read_text(encoding="utf-8")
            lines = content.count("\n") + 1
            return f"[{path} - {lines} lines]\n{content}"
        except Exception as e: return f"ERROR: {e}"

    elif tool_name == "str_replace":
        if not filepath.exists(): return f"ERROR: Not found: {path}"
        try: content = filepath.read_text(encoding="utf-8")
        except Exception as e: return f"ERROR: {e}"

        old_str = tool_input["old_str"]
        new_str = tool_input["new_str"]
        count = content.count(old_str)

        if count == 0:
            # Show what's ACTUALLY in the file near where they expected
            first_line = old_str.splitlines()[0].strip()[:40] if old_str.strip() else ""
            lines = content.splitlines()
            nearby = []
            for i, l in enumerate(lines):
                # Check for partial matches
                if first_line[:15] and first_line[:15] in l:
                    nearby.append(f"  {i+1}: {l}")
            hint = "\n".join(nearby[:8]) if nearby else "  (no similar lines)"
            return (f"ERROR: old_str not found in {path}. The file may have changed from previous edits.\n"
                    f"Use read_file to see the CURRENT file content, then retry with the exact text.\n"
                    f"Searching for: {first_line}\n"
                    f"Similar lines in current file:\n{hint}")

        if count > 1:
            return f"ERROR: Found {count} matches. Include more surrounding lines in old_str to make it unique."

        # Find position for context
        pos = content.index(old_str)
        content = content.replace(old_str, new_str, 1)
        filepath.write_text(content, encoding="utf-8")

        # Return surrounding context so agent sees the current state
        ctx = get_surrounding_context(content, pos + len(new_str) // 2)
        return f"OK: Replaced in {path}.\nCurrent file around edit:\n{ctx}"

    elif tool_name == "create_file":
        if filepath.exists(): return f"ERROR: Already exists: {path}. Use str_replace."
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(tool_input["content"], encoding="utf-8")
        return f"OK: Created {path} ({tool_input['content'].count(chr(10))+1} lines)"

    elif tool_name == "insert_at":
        if not filepath.exists(): return f"ERROR: Not found: {path}"
        try: content = filepath.read_text(encoding="utf-8")
        except Exception as e: return f"ERROR: {e}"

        after = tool_input["after"]
        count = content.count(after)
        if count == 0:
            first_line = after.splitlines()[0].strip()[:40] if after.strip() else ""
            return (f"ERROR: Anchor not found in {path}. File may have changed.\n"
                    f"Use read_file to see current content.\nLooking for: {first_line}")
        if count > 1:
            return f"ERROR: Anchor found {count} times. Include more context to make it unique."

        pos = content.index(after) + len(after)
        content = content[:pos] + "\n" + tool_input["content"] + content[pos:]
        filepath.write_text(content, encoding="utf-8")

        ctx = get_surrounding_context(content, pos + len(tool_input["content"]) // 2)
        return f"OK: Inserted in {path}.\nCurrent file around insertion:\n{ctx}"

    elif tool_name == "write_file":
        if not filepath.exists(): return f"ERROR: Not found: {path}. Use create_file for new files."
        try:
            new_content = tool_input["content"]
            filepath.write_text(new_content, encoding="utf-8")
            lines = new_content.count("\n") + 1
            return f"OK: Wrote {path} ({lines} lines)"
        except Exception as e: return f"ERROR: {e}"

    return f"ERROR: Unknown tool: {tool_name}"


# ---------------------------------------------------------------------------
# Coder Agentic Loop
# ---------------------------------------------------------------------------
def run_coder_with_tools(client, project_dir, design_doc, project_context_light, preloaded_files):
    system = load_prompt("coder")
    messages = [
        {"role": "user", "content":
            f"## PROJECT STRUCTURE\n{project_context_light}\n\n---\n\n"
            f"{preloaded_files}\n\n---\n\n"
            f"## DESIGN DOCUMENT\n\n{design_doc}\n\n"
            f"Implement the feature. The target files are pre-loaded above.\n"
            f"For large files with many changes, use write_file to rewrite the whole file.\n"
            f"For small targeted edits, use str_replace one at a time.\n"
            f"When done, summarize what you changed."}
    ]

    print(f"\n{'='*60}\n  RUNNING: CODER AGENT (with tools)\n{'='*60}")
    start = time.time()
    total_calls = 0
    files_modified = set()
    files_created = set()
    actions = []
    consecutive_fails = 0
    max_iterations = 40
    max_consecutive_fails = 5

    system_msg=[{"type":"text","text":system,"cache_control":{"type":"ephemeral"}}]

    for iteration in range(max_iterations):
        response = client.messages.create(
            model=MODEL, max_tokens=CODER_MAX_TOKENS, system=system_msg,
            tools=CODER_TOOLS, messages=messages)

        tool_results = []
        has_tools = False
        text_parts = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                has_tools = True
                total_calls += 1
                tn = block.name
                ti = block.input
                p = ti.get("path", "")

                if tn == "read_file":
                    print(f"    Reading: {p}")
                elif tn == "str_replace":
                    old_preview = ti.get('old_str','')[:60].replace('\n',' ')
                    print(f"    Editing: {p} - {old_preview}...")
                elif tn == "create_file":
                    print(f"    Creating: {p}")
                elif tn == "insert_at":
                    print(f"    Inserting in: {p}")
                elif tn == "write_file":
                    print(f"    Writing: {p} (full file)")

                result = execute_tool(tn, ti, project_dir)

                if "ERROR" in result:
                    consecutive_fails += 1
                    print(f"    WARN: {result.splitlines()[0]}")
                    if consecutive_fails >= max_consecutive_fails:
                        print(f"    WARN: {max_consecutive_fails} consecutive failures. Forcing re-read.")
                        result += f"\n\nYou have failed {max_consecutive_fails} times in a row. You MUST call read_file now to see the current file state before trying any more edits."
                        consecutive_fails = 0
                else:
                    consecutive_fails = 0
                    if tn in ("str_replace", "insert_at", "write_file"): files_modified.add(p)
                    elif tn == "create_file": files_created.add(p)

                actions.append(f"{tn}({p}): {'ERROR' if 'ERROR' in result else 'OK'}")

                # Truncate read_file results
                if tn == "read_file" and len(result) > 25000:
                    result = result[:25000] + "\n... (truncated)"

                tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":result})

        if not has_tools:
            break

        messages.append({"role":"assistant","content":response.content})
        messages.append({"role":"user","content":tool_results})

    elapsed = time.time() - start
    final_text = "\n".join(text_parts) if text_parts else ""
    print(f"\n  Done in {elapsed:.1f}s | {total_calls} tool calls")
    print(f"  Modified: {', '.join(files_modified) or 'none'}")
    print(f"  Created:  {', '.join(files_created) or 'none'}")

    summary = f"## Coder Summary\n\n{final_text}\n\n"
    summary += f"### Files Modified\n" + "\n".join(f"- {f}" for f in files_modified) + "\n"
    if files_created: summary += f"### Files Created\n" + "\n".join(f"- {f}" for f in files_created) + "\n"
    summary += f"\n### Actions ({len(actions)} total)\n" + "\n".join(f"- {a}" for a in actions) + "\n"

    return summary, list(files_modified), list(files_created)


# ---------------------------------------------------------------------------
# Other Agents
# ---------------------------------------------------------------------------
def load_prompt(n):
    p=PROMPTS_DIR/f"{n}.txt"
    if not p.exists(): print(f"  ERROR: {p} not found"); sys.exit(1)
    return p.read_text(encoding="utf-8")

def call_agent(client,name,sp,um):
    print(f"\n{'='*60}\n  RUNNING: {name.upper()} AGENT\n{'='*60}")
    start=time.time()
    # Use prompt caching: mark system prompt and user message for caching
    system_msg=[{"type":"text","text":sp,"cache_control":{"type":"ephemeral"}}]
    user_msg=[{"type":"text","text":um,"cache_control":{"type":"ephemeral"}}]
    r=client.messages.create(model=MODEL,max_tokens=MAX_TOKENS,system=system_msg,
        messages=[{"role":"user","content":user_msg}])
    res=r.content[0].text
    usage=r.usage
    cache_info=""
    if hasattr(usage,"cache_creation_input_tokens") and usage.cache_creation_input_tokens:
        cache_info+=f" | cache write: {usage.cache_creation_input_tokens}"
    if hasattr(usage,"cache_read_input_tokens") and usage.cache_read_input_tokens:
        cache_info+=f" | cache hit: {usage.cache_read_input_tokens}"
    print(f"  Done in {time.time()-start:.1f}s | {usage.input_tokens} in / {usage.output_tokens} out{cache_info}")
    return res

def save_output(d,name,content):
    p=d/f"{name}_output.md"; p.write_text(content,encoding="utf-8"); print(f"  Saved: {p}")

def extract_target_files(design_doc, project_dir):
    """Parse architect output to find file paths mentioned for modification."""
    files = []
    # Match common patterns: "### path/to/file", "- path/to/file.ext", "modify X in file.py", etc.
    for line in design_doc.splitlines():
        # Look for file paths with extensions
        for m in re.finditer(r'[`*]*([a-zA-Z0-9_./\\-]+\.[a-zA-Z]{1,5})[`*]*', line):
            candidate = m.group(1).replace("\\", "/")
            fp = project_dir / candidate
            if fp.exists() and fp.is_file():
                if candidate not in files:
                    files.append(candidate)
    return files

def preload_files(project_dir, file_paths):
    """Read target files and format them for the coder's context."""
    lm={".py":"python",".js":"javascript",".jsx":"jsx",".ts":"typescript",".tsx":"tsx",
        ".html":"html",".htm":"html",".css":"css",".scss":"scss",".json":"json"}
    parts = ["## Pre-loaded Target Files\nThese files are identified by the architect as needing changes.\n"]
    for rel_path in file_paths:
        fp = project_dir / rel_path
        if not fp.exists():
            continue
        try:
            content = fp.read_text(encoding="utf-8")
            lines = content.count("\n") + 1
            lang = lm.get(fp.suffix.lower(), "")
            parts.append(f"### {rel_path} ({lines} lines)\n```{lang}\n{content}\n```\n")
        except:
            pass
    if len(parts) == 1:
        parts.append("*No target files could be pre-loaded. Use read_file to access files.*\n")
    return "\n".join(parts)

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run_pipeline(feature_idea, project_dir, max_revisions=2, auto_apply=False, quick=False):
    client=anthropic.Anthropic()
    ts=datetime.now().strftime("%Y%m%d_%H%M%S")
    out=project_dir/".agent-pipeline"/f"run_{ts}"
    out.mkdir(parents=True,exist_ok=True)

    print("\n  Scanning project...")
    ctx_light=scan_project_light(project_dir)
    if not quick:
        ctx_full=scan_project(project_dir)
        (out/"project_context.md").write_text(ctx_full,encoding="utf-8")
    else:
        (out/"project_context.md").write_text(ctx_light,encoding="utf-8")
    (out/"feature_idea.txt").write_text(feature_idea,encoding="utf-8")

    cur=git_current_branch(project_dir) if git_is_repo(project_dir) else None
    on_feat=cur and is_feature_branch(cur)

    print(f"  Project:  {project_dir.name}")
    print(f"  Feature:  {feature_idea[:80]}{'...' if len(feature_idea)>80 else ''}")
    if quick:
        print(f"  Mode:     Quick (Coder -> Supervisor, no architect)")
        print(f"  Branch:   {cur+' (iterating)' if on_feat else 'New branch'}")
    elif auto_apply:
        print(f"  Mode:     Apply (Architect -> Coder -> Supervisor)")
        print(f"  Branch:   {cur+' (iterating)' if on_feat else 'New branch'}")
    else:
        print(f"  Mode:     Full review (all 5 agents)")
    print(f"  Outputs:  {out}\n")

    # Git setup BEFORE coder runs
    if auto_apply or quick:
        if not git_is_repo(project_dir): git_init_repo(project_dir)
        if on_feat:
            branch=cur; info=load_branch_info(project_dir); orig=info["original_branch"] if info else "main"
            print(f"  Continuing on: {branch}")
        else:
            if git_has_changes(project_dir): git_commit(project_dir,"WIP: before agent-pipeline")
            orig=git_current_branch(project_dir); branch=make_branch_name(feature_idea)
            if not git_create_branch(project_dir,branch): print("  ERROR: branch failed"); return
            save_branch_info(project_dir,orig,branch); print(f"  Created branch: {branch}")

    if quick:
        # Quick mode: skip architect, scan all source files for coder context
        tree, all_src = scan_file_tree(project_dir)
        preloaded = scan_source_files(project_dir, all_src)
        dd = f"## Feature Request\n{feature_idea}\n\nImplement this directly. Follow existing patterns."
        save_output(out,"1_architect",f"*Skipped (quick mode)*\n\n{dd}")
    else:
        # Architect (gets full context with all source files)
        pb_full=(f"## EXISTING PROJECT\nMake targeted modifications only.\n\n{ctx_full}")
        dd=call_agent(client,"architect",load_prompt("architect"),f"{pb_full}\n\n---\n\n## FEATURE REQUEST\n\n{feature_idea}")
        save_output(out,"1_architect",dd)

        # Extract target files from architect output and pre-load them
        target_files = extract_target_files(dd, project_dir)
        if target_files:
            print(f"  Target files: {', '.join(target_files)}")
        preloaded = preload_files(project_dir, target_files)

    # Coder (gets light context + pre-loaded target files only)
    coder_summary, modified, created = run_coder_with_tools(client, project_dir, dd, ctx_light, preloaded)
    save_output(out, "2_coder", coder_summary)

    # Tester + Security in full review (run in parallel)
    if not auto_apply and not quick:
        from concurrent.futures import ThreadPoolExecutor
        pb_full=(f"## EXISTING PROJECT\nMake targeted modifications only.\n\n{ctx_full}")
        tester_msg=f"{pb_full}\n\n---\n\n## DESIGN\n\n{dd}\n\n---\n\n## CHANGES\n\n{coder_summary}\n\nTests."
        security_msg=f"{pb_full}\n\n---\n\n## DESIGN\n\n{dd}\n\n---\n\n## CHANGES\n\n{coder_summary}\n\nSecurity."
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_tester=pool.submit(call_agent,client,"tester",load_prompt("tester"),tester_msg)
            fut_security=pool.submit(call_agent,client,"security",load_prompt("security"),security_msg)
            to=fut_tester.result()
            so=fut_security.result()
        save_output(out,"3_tester",to)
        save_output(out,"4_security",so)
        sup_extra=f"\n## TESTS\n\n{to}\n\n---\n\n## SECURITY\n\n{so}"
    else:
        sup_extra="\nFast mode. Focus: correct implementation, targeted changes, existing features preserved."

    # Supervisor (gets light context -- doesn't need full source, just design + changes)
    sup=call_agent(client,"supervisor",load_prompt("supervisor"),
        f"## EXISTING PROJECT\n{ctx_light}\n\n---\n\n## DESIGN\n\n{dd}\n\n---\n\n## CHANGES\n\n{coder_summary}\n\n---{sup_extra}\n\nVerdict.")
    save_output(out,"5_supervisor",sup)

    approved="APPROVED" in sup.upper()
    print(f"\n{'='*60}\n  PIPELINE: {'APPROVED' if approved else 'NOT APPROVED'}")

    if auto_apply or quick:
        if approved and (modified or created):
            git_commit(project_dir,f"feat: {feature_idea[:60]}\n\nby agent-pipeline | {out.name}")
            print(f"\n  {'='*55}\n  CHANGES APPLIED ON: {branch}\n  {'='*55}")
            print(f"\n  Files changed:")
            for f in modified: print(f"    Modified: {f}")
            for f in created: print(f"    Created:  {f}")
            print(f"\n  Next steps:")
            print(f"    Check it       -> Browser / VS Code")
            print(f"    Iterate        -> agent-pipeline --apply \"tweak\"")
            print(f"    Merge (+ sec)  -> agent-pipeline --merge")
            print(f"    Rollback       -> agent-pipeline --rollback")
        elif approved:
            print("\n  Approved but no files changed.")
        else:
            print("\n  Not approved. Reverting changes...")
            git_run(project_dir, "checkout", ".")
            if not on_feat:
                git_checkout(project_dir,orig); git_delete_branch(project_dir,branch)
                p=project_dir/BRANCH_INFO_FILE
                if p.exists(): p.unlink()
            print("  Reverted.")

    print(f"\n  Outputs: {out}\n{'='*60}\n")

def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("\n  ERROR: ANTHROPIC_API_KEY not set.")
        print(f"  Add it to {SCRIPT_DIR / '.env'} or set it in your environment.\n")
        sys.exit(1)
    args=sys.argv[1:]; d=Path.cwd()
    if not args:
        print("\n  5-Agent Coding Pipeline")
        print("  "+"="*40)
        print('\n  Commands:')
        print('    agent-pipeline "idea"              Full review (5 agents)')
        print('    agent-pipeline --apply "idea"      Fast apply (3 agents + file tools)')
        print('    agent-pipeline --quick "idea"      Quick edit (Coder + Supervisor only)')
        print('    agent-pipeline --merge             Security review + merge')
        print('    agent-pipeline --rollback          Discard feature branch')
        print('\n  Options:')
        print('    --yes, -y                          Skip confirmation prompts')
        print('\n  Workflow:')
        print('    1. agent-pipeline --apply "add a modal"')
        print('    2. Check browser / VS Code')
        print('    3. Tweak  -> agent-pipeline --apply "fix the modal"')
        print('    4. Done  -> agent-pipeline --merge')
        print('    5. Undo  -> agent-pipeline --rollback\n')
        sys.exit(1)
    if "--merge" in args: do_merge(d); return
    if "--rollback" in args: do_rollback(d); return
    auto="--apply" in args
    quick="--quick" in args
    yes="--yes" in args or "-y" in args
    if auto: args.remove("--apply")
    if quick: args.remove("--quick")
    if "--yes" in args: args.remove("--yes")
    if "-y" in args: args.remove("-y")
    if not args: print("  No feature idea."); sys.exit(1)
    feature=" ".join(args)
    ind=["package.json","src","app","pages","components","requirements.txt","pyproject.toml","setup.py","manage.py","Pipfile","main.py","app.py","index.html"]
    if not any((d/i).exists() for i in ind):
        if yes: pass
        elif input("  Not a project dir. Continue? (y/n): ").strip().lower()!="y": sys.exit(0)
    if auto or quick:
        cur=git_current_branch(d) if git_is_repo(d) else None
        if cur and is_feature_branch(cur): print(f"\n  Iterating on: {cur}")
        else: print(f"\n  New branch will be created.")
        if not yes:
            if input("  Continue? (y/n): ").strip().lower()!="y": sys.exit(0)
    run_pipeline(feature,d,auto_apply=auto,quick=quick)

if __name__=="__main__": main()
