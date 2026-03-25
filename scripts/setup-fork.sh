#!/usr/bin/env bash
set -euo pipefail

# Setup a forked repository with a known bug for Ralph Loop testing.
# Usage: ./scripts/setup-fork.sh <source_repo> <fork_org> <rollback_commit> <issue_url>
#
# Example:
#   ./scripts/setup-fork.sh konflux-ci/build-service my-org abc123 https://github.com/konflux-ci/build-service/issues/42
#
# Prerequisites:
#   - gh CLI installed and authenticated
#   - GH_PAT or GITHUB_TOKEN set for API access

SOURCE_REPO="${1:?Usage: setup-fork.sh <source_repo> <fork_org> <rollback_commit> <issue_url>}"
FORK_ORG="${2:?Specify the org/user to fork into}"
ROLLBACK_COMMIT="${3:?Specify the commit hash to roll back to (before the human fix)}"
ISSUE_URL="${4:?Specify the issue URL}"

REPO_NAME=$(echo "$SOURCE_REPO" | cut -d'/' -f2)

echo "=== Ralph Loop Test Repo Setup ==="
echo "Source: $SOURCE_REPO"
echo "Fork to: $FORK_ORG/$REPO_NAME"
echo "Rollback to: $ROLLBACK_COMMIT"
echo "Issue: $ISSUE_URL"
echo ""

# Fork the repo
echo "Forking $SOURCE_REPO to $FORK_ORG..."
gh repo fork "$SOURCE_REPO" --org "$FORK_ORG" --clone=false || echo "Fork may already exist"

# Clone the fork
echo "Cloning fork..."
FORK_URL="https://github.com/$FORK_ORG/$REPO_NAME.git"
CLONE_DIR="/tmp/rl-test-$REPO_NAME"
rm -rf "$CLONE_DIR"
git clone "$FORK_URL" "$CLONE_DIR"
cd "$CLONE_DIR"

# Create a test branch rolled back to before the fix
BRANCH_NAME="rl-test/bug-$(date +%Y%m%d)"
echo "Creating branch $BRANCH_NAME at $ROLLBACK_COMMIT..."
git checkout -b "$BRANCH_NAME" "$ROLLBACK_COMMIT"
git push origin "$BRANCH_NAME"

echo ""
echo "=== Setup Complete ==="
echo "Fork URL: https://github.com/$FORK_ORG/$REPO_NAME"
echo "Branch: $BRANCH_NAME"
echo "Issue URL: $ISSUE_URL"
echo "Clone directory: $CLONE_DIR"
echo ""
echo "To run the Ralph Loop against this:"
echo "  rl-engine --issue-url '$ISSUE_URL' --target-repo '$CLONE_DIR' --output-dir ./output"
