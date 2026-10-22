# Shared skills

Keep canonical skill files here. Category directories mirror the source packages in
`agents/`: augmentation, cleaning, filter, mcp_interface, metadata, and transformation.
The `.gitkeep` files preserve empty categories in Git; remove them when adding skills.

Each skill is a directory containing `SKILL.md`, for example:

```text
.agents/skills/
└── transformation/
    └── biolink-mapping/
        ├── SKILL.md
        ├── references/
        └── scripts/
```

Use a globally unique leaf directory name, with matching `name` frontmatter and a
nonempty `description`. Category names are **not** part of the skill name:

```markdown
---
name: biolink-mapping
description: Map source entities and relationships to Biolink types and predicates.
---

Instructions go here. Reference supporting files relative to this directory.
```

Pi and the installed Pi subagents extension discover grouped skills recursively
under this real directory. To expose them to Claude Code as flat directory entries:

```bash
python3 scripts/sync_claude_skills.py --dry-run
python3 scripts/sync_claude_skills.py
```

The script works from any working directory and creates relative links such as:

```text
.claude/skills/biolink-mapping -> ../../.agents/skills/transformation/biolink-mapping
```

Both harnesses can then reference `biolink-mapping` without a category prefix.
Pi agent frontmatter uses `skills: biolink-mapping`; Claude agent frontmatter can use:

```yaml
skills:
  - biolink-mapping
```

The script is repeatable, skips already-correct links, and validates all name
collisions before creating links. It refuses to overwrite files, directories,
incorrect links, or broken links by default. It also refuses a symlinked
`.claude/skills` root: use a real directory containing individual skill links instead.

To clean up broken links after deleting, renaming, or moving skills, opt into pruning:

```bash
python3 scripts/sync_claude_skills.py --prune --dry-run
python3 scripts/sync_claude_skills.py --prune
```

Only broken symlinks whose resolved targets are inside this project's
`.agents/skills/` are removed. Real files/directories, live links, and unrelated
broken links are preserved. A broken managed link with a name matching a moved
skill is removed and recreated at the new target. All collisions are validated
before any links are created or removed. Links to existing directories that no
longer contain `SKILL.md` are not broken and must be reviewed manually.

Source skill symlinks are not followed, to match the Pi subagent preloader's
restrictions. A skill's supporting directories are not searched for further skills.

Run the script again after adding/moving skills. Reload Pi with `/reload` and restart
Claude Code, then verify discovery and subagent preloading in your installed version;
Claude's symlink behavior is not tested by this repository's filesystem tests.
