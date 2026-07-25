---
name: commit
description: >
  Create a git commit using the Conventional Commits specification. Use when the developer asks to: commit changes,
  create a commit, stage and commit, write a commit message, or make a conventional commit. Triggers on "commit",
  "git commit", "stage changes", "commit message", "conventional commit", or "save my changes".
disable-model-invocation: true
allowed-tools: Bash(git *)
---

# Conventional Commit

Create commits following the [Conventional Commits](https://www.conventionalcommits.org/) specification.

## Format

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

## Commit Types

| Type       | Description                                           |
|------------|-------------------------------------------------------|
| `feat`     | A new feature                                         |
| `fix`      | A bug fix                                             |
| `docs`     | Documentation only changes                            |
| `style`    | Changes that do not affect meaning (formatting, etc.) |
| `refactor` | Code change that neither fixes a bug nor adds feature |
| `perf`     | Performance improvement                               |
| `test`     | Adding or correcting tests                            |
| `build`    | Changes to build system or dependencies               |
| `ci`       | Changes to CI configuration                           |
| `config`   | Configuration only changes                            |
| `chore`    | Other changes that don't modify src or test files     |
| `revert`   | Reverts a previous commit                             |

## Workflow

1. Run `git status` to see all untracked and modified files (never use `-uall`)
2. Run `git diff` to see staged and unstaged changes
3. Run `git log --oneline -5` to see recent commit style
4. Run `git rev-parse --abbrev-ref HEAD` and derive the GitHub issue footer (see [GitHub issue footer](#github-issue-footer))
5. Analyze changes and determine the appropriate type
6. Stage specific files by name (avoid `git add -A` or `git add .`)
7. Create commit with HEREDOC format (omit the `Refs:` line when no ticket key is found):

```bash
git commit -m "$(cat <<'EOF'
<type>[scope]: <description>

[body if needed]

Refs: #12
EOF
)"
```

8. Run `git status` to verify success

## GitHub Issue Footer

Reference the GitHub issue in the commit footer so the work stays traceable from git history.

In this repo, agent branches follow `ai/<n>` where `<n>` is the issue number, so derive it from the current branch:

1. Run `git rev-parse --abbrev-ref HEAD` to get the branch name.
2. Match `ai/([0-9]+)` against it — the capture is the issue number (e.g. `ai/12` → `#12`).
3. Append the footer as the last line, separated from the subject (or body, if present) by one blank line:

   ```
   Refs: #12
   ```

If the branch name contains no issue number (e.g. on `main`), omit the footer entirely — **never block the commit**. Do not prompt for the number: a wrong reference is visible in the commit output and is fixed with a new commit.

## Repo-Specific Rules (ia-orchestrator)

The rules from this repo's `CLAUDE.md` take precedence: commit with the personal identity already set in the local git config, and never add a `Co-Authored-By` line. Commit messages may be written in French (see `git log` for the prevailing style).

## Safety Rules

- NEVER update git config
- NEVER run destructive commands (push --force, reset --hard, etc.) unless explicitly requested
- NEVER skip hooks (--no-verify) unless explicitly requested
- ALWAYS create NEW commits - never amend unless explicitly requested
- NEVER commit sensitive files (.env, credentials, secrets)
- When a pre-commit hook fails, fix the issue and create a NEW commit

## Examples

**Feature:**
```
feat(auth): add password reset functionality
```

**Bug fix with scope:**
```
fix(api): handle null response from user endpoint
```

**Breaking change:**
```
feat(parser)!: remove deprecated options

BREAKING CHANGE: The `--legacy` flag has been removed.
```

**Multiple-line body:**
```
fix(database): resolve connection pool exhaustion

The connection pool was not properly releasing connections
after timeout errors. Added explicit cleanup in finally block.
```

**With GitHub issue reference (branch `ai/12`):**
```
feat(poll): add per-repo config support

Refs: #12
```
