#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# ///

import json
import sys
import re
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).parent / "utils"))
    from session_tracker import track
except Exception:  # the session tracker must never break this hook
    def track(event, payload):
        return None

try:
    from session_tracker import load_config, restart_gate
except Exception:  # no gate rather than a broken hook
    restart_gate = None

try:
    from session_tracker import verify_gate
except Exception:  # no gate rather than a broken hook
    verify_gate = None

try:
    from session_tracker import landing_guard
except Exception:  # no guard rather than a broken hook
    landing_guard = None

def is_dangerous_rm_command(command):
    """
    Comprehensive detection of dangerous rm commands.
    Matches various forms of rm -rf and similar destructive patterns.
    """
    # Normalize command by removing extra spaces and converting to lowercase
    normalized = ' '.join(command.lower().split())
    
    # Pattern 1: Standard rm -rf variations
    patterns = [
        r'\brm\s+.*-[a-z]*r[a-z]*f',  # rm -rf, rm -fr, rm -Rf, etc.
        r'\brm\s+.*-[a-z]*f[a-z]*r',  # rm -fr variations
        r'\brm\s+--recursive\s+--force',  # rm --recursive --force
        r'\brm\s+--force\s+--recursive',  # rm --force --recursive
        r'\brm\s+-r\s+.*-f',  # rm -r ... -f
        r'\brm\s+-f\s+.*-r',  # rm -f ... -r
    ]
    
    # Check for dangerous patterns
    for pattern in patterns:
        if re.search(pattern, normalized):
            return True
    
    # Pattern 2: Check for rm with recursive flag targeting dangerous paths
    dangerous_paths = [
        r'/',           # Root directory
        r'/\*',         # Root with wildcard
        r'~',           # Home directory
        r'~/',          # Home directory path
        r'\$HOME',      # Home environment variable
        r'\.\.',        # Parent directory references
        r'\*',          # Wildcards in general rm -rf context
        r'\.',          # Current directory
        r'\.\s*$',      # Current directory at end of command
    ]
    
    if re.search(r'\brm\s+.*-[a-z]*r', normalized):  # If rm has recursive flag
        for path in dangerous_paths:
            if re.search(path, normalized):
                return True
    
    return False

def is_env_file_access(tool_name, tool_input):
    """
    Check if any tool is trying to access .env files containing sensitive data.
    """
    if tool_name in ['Read', 'Edit', 'MultiEdit', 'Write', 'Bash']:
        # Check file paths for file-based tools
        if tool_name in ['Read', 'Edit', 'MultiEdit', 'Write']:
            file_path = tool_input.get('file_path', '')
            if '.env' in file_path and not file_path.endswith('.env.sample'):
                return True
        
        # Check bash commands for .env file access
        elif tool_name == 'Bash':
            command = tool_input.get('command', '')
            # Pattern to detect .env file access (but allow .env.sample)
            env_patterns = [
                r'\b\.env\b(?!\.sample)',  # .env but not .env.sample
                r'cat\s+.*\.env\b(?!\.sample)',  # cat .env
                r'echo\s+.*>\s*\.env\b(?!\.sample)',  # echo > .env
                r'touch\s+.*\.env\b(?!\.sample)',  # touch .env
                r'cp\s+.*\.env\b(?!\.sample)',  # cp .env
                r'mv\s+.*\.env\b(?!\.sample)',  # mv .env
            ]
            
            for pattern in env_patterns:
                if re.search(pattern, command):
                    return True
    
    return False

LARGE_READ_BYTES = 50_000
NON_TEXT_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.ipynb'}

def large_whole_file_read(tool_name, tool_input):
    """
    Return the size of a text file being Read whole (no offset/limit) when it is over LARGE_READ_BYTES.
    Once read, a file is re-sent on every later call of the session.
    """
    if tool_name != 'Read' or tool_input.get('offset') or tool_input.get('limit') or tool_input.get('allow_large'):
        return 0
    path = Path(tool_input.get('file_path', ''))
    if path.suffix.lower() in NON_TEXT_SUFFIXES:
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    return size if size > LARGE_READ_BYTES else 0

def main():
    try:
        # Read JSON input from stdin
        input_data = json.load(sys.stdin)
        
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})
        if tool_name in ('AskUserQuestion', 'Bash'):  # Bash: HEAD snapshot, so only its own commits count
            track("PreToolUse", input_data)
        if tool_name == 'Bash' and restart_gate is not None:  # restarts are requested from the /cc session (CC9)
            try:
                reason = restart_gate(input_data, load_config())
            except Exception:
                reason = None
            if reason:
                print(reason, file=sys.stderr)
                sys.exit(2)
        if tool_name == 'Bash' and landing_guard is not None:  # no code lands on main mid-restart (CC14)
            try:
                reason = landing_guard(input_data, load_config())
            except Exception:
                reason = None
            if reason:
                print(reason, file=sys.stderr)
                sys.exit(2)
        if tool_name == 'Bash' and verify_gate is not None:  # a restart ships only verified code (owner 11-01)
            try:
                reason = verify_gate(input_data, load_config())
            except Exception:
                reason = None
            if reason:
                print(reason, file=sys.stderr)
                sys.exit(2)
        
        # Check for .env file access (blocks access to sensitive environment files)
        if is_env_file_access(tool_name, tool_input):
            print("BLOCKED: Access to .env files containing sensitive data is prohibited", file=sys.stderr)
            print("Use .env.sample for template files instead", file=sys.stderr)
            sys.exit(2)  # Exit code 2 blocks tool call and shows error to Claude

        # Block whole-file reads of large files (they ride along on every later call)
        size = large_whole_file_read(tool_name, tool_input)
        if size:
            file_path = tool_input.get('file_path', '')
            print(f"BLOCKED: {file_path} is {size // 1000} KB; a whole-file Read stays in context for the rest "
                  f"of the session.", file=sys.stderr)
            print("Grep for the lines you need, then Read with offset/limit; or ask about the file without "
                  f"reading it: python3 /opt/claude-code-hooks-mastery/scripts/rlm.py \"<question>\" {file_path}",
                  file=sys.stderr)
            print("If the whole file is truly needed, Read again with allow_large: true.", file=sys.stderr)
            sys.exit(2)

        # Check for dangerous rm -rf commands
        if tool_name == 'Bash':
            command = tool_input.get('command', '')
            
            # Block rm -rf commands with comprehensive pattern matching
            if is_dangerous_rm_command(command):
                print("BLOCKED: Dangerous rm command detected and prevented", file=sys.stderr)
                sys.exit(2)  # Exit code 2 blocks tool call and shows error to Claude
        
        # Ensure log directory exists
        log_dir = Path.cwd() / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / 'pre_tool_use.json'
        
        # Read existing log data or initialize empty list
        if log_path.exists():
            with open(log_path, 'r') as f:
                try:
                    log_data = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    log_data = []
        else:
            log_data = []
        
        # Append new data
        log_data.append(input_data)
        
        # Write back to file with formatting
        with open(log_path, 'w') as f:
            json.dump(log_data, f, indent=2)
        
        sys.exit(0)
        
    except json.JSONDecodeError:
        # Gracefully handle JSON decode errors
        sys.exit(0)
    except Exception:
        # Handle any other errors gracefully
        sys.exit(0)

if __name__ == '__main__':
    main()