#!/usr/bin/env bash
set -euo pipefail

# Setup a forked repository with a known bug for Ralph Loop testing.
#
# Forks a Konflux repo, rolls back to before a specified fix commit, and
# outputs both human-readable and machine-readable (JSON) setup summaries.
#
# Usage:
#   ./scripts/setup-fork.sh <source_repo> <fork_org> <rollback_commit> <issue_url>
#
# Example:
#   ./scripts/setup-fork.sh konflux-ci/build-service my-org abc123 \
#       https://github.com/konflux-ci/build-service/issues/42
#
# Prerequisites:
#   - gh CLI installed and authenticated
#   - git installed
#
# Outputs:
#   - A forked repository with a test branch at the rollback commit
#   - A JSON summary at <clone_dir>/rl-setup.json

USAGE="Usage: setup-fork.sh <source_repo> <fork_org> <rollback_commit> <issue_url>"

SOURCE_REPO="${1:?$USAGE}"
FORK_ORG="${2:?Specify the org/user to fork into. $USAGE}"
ROLLBACK_COMMIT="${3:?Specify the commit hash to roll back to (before the human fix). $USAGE}"
ISSUE_URL="${4:?Specify the issue URL. $USAGE}"

REPO_NAME=$(echo "$SOURCE_REPO" | cut -d'/' -f2)

err() { echo "ERROR: $*" >&2; exit 1; }

# --- Prerequisite checks ---

command -v gh >/dev/null 2>&1 || err "gh CLI not found. Install from https://cli.github.com/"
command -v git >/dev/null 2>&1 || err "git not found."

if ! gh auth status >/dev/null 2>&1; then
    err "gh CLI not authenticated. Run 'gh auth login' first."
fi

if [[ ! "$SOURCE_REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
    err "Invalid source_repo format '$SOURCE_REPO'. Expected 'owner/repo'."
fi

if [[ ! "$ISSUE_URL" =~ ^https://github\.com/.+/issues/[0-9]+$ ]]; then
    err "Invalid issue URL '$ISSUE_URL'. Expected https://github.com/<owner>/<repo>/issues/<number>."
fi

echo "=== Ralph Loop Test Repo Setup ==="
echo "Source:      $SOURCE_REPO"
echo "Fork to:     $FORK_ORG/$REPO_NAME"
echo "Rollback to: $ROLLBACK_COMMIT"
echo "Issue:       $ISSUE_URL"
echo ""

# --- Fork the repo ---

echo "Forking $SOURCE_REPO to $FORK_ORG..."
if ! gh repo fork "$SOURCE_REPO" --org "$FORK_ORG" --clone=false 2>/dev/null; then
    echo "  (fork may already exist, continuing)"
fi

# --- Clone the fork ---

FORK_URL="https://github.com/$FORK_ORG/$REPO_NAME.git"
CLONE_DIR="/tmp/rl-test-$REPO_NAME"

echo "Cloning $FORK_URL..."
rm -rf "$CLONE_DIR"
if ! git clone --quiet "$FORK_URL" "$CLONE_DIR"; then
    err "Failed to clone $FORK_URL. Does the fork exist at $FORK_ORG/$REPO_NAME?"
fi
cd "$CLONE_DIR"

# --- Verify the rollback commit exists ---

if ! git cat-file -e "$ROLLBACK_COMMIT^{commit}" 2>/dev/null; then
    err "Commit $ROLLBACK_COMMIT not found in the repository. Verify the hash."
fi

# --- Create the test branch ---

BRANCH_NAME="rl-test/bug-$(date +%Y%m%d)"
echo "Creating branch $BRANCH_NAME at $ROLLBACK_COMMIT..."
git checkout -b "$BRANCH_NAME" "$ROLLBACK_COMMIT" --quiet

echo "Pushing $BRANCH_NAME to origin..."
if ! git push origin "$BRANCH_NAME" --quiet 2>/dev/null; then
    err "Failed to push branch. Check push permissions for $FORK_ORG/$REPO_NAME."
fi

# --- Write machine-readable summary ---

SETUP_JSON="$CLONE_DIR/rl-setup.json"
cat > "$SETUP_JSON" <<EOJSON
{
  "source_repo": "$SOURCE_REPO",
  "fork_repo": "$FORK_ORG/$REPO_NAME",
  "fork_url": "https://github.com/$FORK_ORG/$REPO_NAME",
  "branch": "$BRANCH_NAME",
  "rollback_commit": "$ROLLBACK_COMMIT",
  "issue_url": "$ISSUE_URL",
  "clone_dir": "$CLONE_DIR",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOJSON

echo ""
echo "=== Setup Complete ==="
echo "Fork URL:        https://github.com/$FORK_ORG/$REPO_NAME"
echo "Branch:          $BRANCH_NAME"
echo "Issue URL:       $ISSUE_URL"
echo "Clone directory: $CLONE_DIR"
echo "Setup JSON:      $SETUP_JSON"
echo ""
echo "To run the Ralph Loop against this:"
echo "  python -m engine \\"
echo "    --issue-url '$ISSUE_URL' \\"
echo "    --target-repo '$CLONE_DIR' \\"
echo "    --output-dir ./output"
