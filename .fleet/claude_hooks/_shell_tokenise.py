#!/usr/bin/env -S uv run --script
# Fleet-managed copy of fleet payload/claude_hooks/_shell_tokenise.py. Do not edit it here:
#      edit it there, then run `just sync` in fleet.
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Shared shell-command tokeniser for the PreToolUse(Bash) guards.

Every guard that inspects a Bash command string needs the same two things: tokens that
respect quoting the way a shell would, and the command split into per-statement segments
at `;`/`&&`/`||`/`|`/`&` and newlines. Both are harder than `shlex.split()` alone gives you,
so the logic lives here once rather than being reimplemented per guard.

A NEWLINE IS A SEPARATOR TOO, and recovering it is why `parse()` works a line at a time
rather than handing shlex the whole command. shlex treats a newline as ordinary whitespace,
so `bd show x` + newline + `bd create y` would collapse into one segment beginning `bd
show` and a check looking only at the first token per chain would miss the second command
entirely. Splitting on lines and splicing a synthetic `;` between them restores the
boundary, but only once the three things that legitimately span lines are stitched back
together first:

  * a quoted value -- a line that fails to parse is held and retried joined to the next
    with its newline intact, so a multi-line value (a commit message, a `--notes` body)
    reaches callers byte-for-byte rather than being mangled or split into a bogus second
    command;
  * a backslash continuation -- distinguished from the above by `ends_mid_escape()`, and
    dropped the way a shell drops it, so a command whose flag sits on a continuation line
    is not read as missing that flag entirely;
  * a heredoc body -- its lines are data, not commands, so a document being written with
    `cat <<EOF` that quotes a command in its text is not mistaken for running one.

What line segmentation still does not see: a heredoc whose delimiter never appears
swallows the rest of the command, and a quoted word that survives quote-stripping looking
exactly like an operator (`echo "<<EOF"`) starts a body that is not there. A
backslash-newline *inside* double quotes keeps a literal newline where a shell would
remove it, which can only ever alter the inside of a quoted value, never where a command
boundary falls.

`parse()` reports whether anything was left unparsed rather than silently dropping it, so
callers can choose their own failure mode: a guard that only *nudges* (a labelling
convention) can fail open and check what it could parse, while a guard that is a real
boundary (an override-flag block) can fail closed and refuse to run a command it could not
fully account for. `tokenise()` is the fail-open convenience wrapper for the former.

Standard library only, and no third-party imports: these hooks run in their own PEP 723
environment (`dependencies = []`), not the project's, so no project package is importable.
"""

from __future__ import annotations

import re
import shlex
from typing import NamedTuple

# Shell control operators that separate one command from the next. shlex.split returns
# these as their own unquoted tokens, so a segment boundary is just "this token, verbatim,
# outside quotes". A lone "&" backgrounds the command before it and is therefore a
# separator too; "&&", "&>" and "2>&1" tokenise whole, so including it does not split them.
COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})

# The separator spliced in where a newline ended a command, so the segmenter above sees a
# boundary that shlex would otherwise have swallowed as ordinary whitespace.
_SYNTHETIC_SEPARATOR = ";"

# Characters that end one command and start another wherever they stand unquoted, even pressed
# against a word: shlex.split keeps "status;" and "hi&&git" whole, which would hide a boundary.
# "(" and ")" open and close a subshell, "`" a command substitution.
_ALWAYS_SEPARATE = frozenset({";", "(", ")", "`"})
_REDIRECT_CHARS = frozenset({"<", ">"})

# A heredoc redirection: an optional fd, `<<` or `<<-`, and the delimiter word, which may
# instead be the next token. `[^<]` keeps `<<<` (a here-string, which has no body) out.
_HEREDOC_OPERATOR_RE = re.compile(r"^\d*<<(?P<dash>-?)(?P<delimiter>[^<].*)?$")

# Any ordinary character will do: it is only ever appended to a chunk that already failed
# to parse, to tell a dangling backslash apart from an unterminated quote.
_ESCAPE_PROBE_CHAR = "x"


class Heredoc(NamedTuple):
    delimiter: str
    strips_tabs: bool


class Parse(NamedTuple):
    """The result of tokenising a command: what parsed, and whether anything did not."""

    tokens: list[str]
    truncated: bool


def heredocs_opened(tokens: list[str]) -> list[Heredoc]:
    """The heredoc bodies `tokens` queues up, in the order the shell will consume them."""
    opened = []
    awaiting_delimiter = False
    awaited_strips_tabs = False
    for token in tokens:
        if awaiting_delimiter:
            opened.append(Heredoc(delimiter=token, strips_tabs=awaited_strips_tabs))
            awaiting_delimiter = False
            continue
        match = _HEREDOC_OPERATOR_RE.match(token)
        if not match:
            continue
        awaited_strips_tabs = bool(match.group("dash"))
        delimiter = match.group("delimiter") or ""
        if delimiter:
            opened.append(Heredoc(delimiter=delimiter, strips_tabs=awaited_strips_tabs))
        else:
            awaiting_delimiter = True
    return opened


def ends_mid_escape(chunk: str) -> bool:
    """True if `chunk` breaks off on a backslash outside any quote -- a line continuation.

    shlex cannot answer this directly: an unterminated quote and a dangling backslash both
    raise ValueError. Appending one ordinary character separates them, because it satisfies
    a dangling escape but leaves an open quote just as open.
    """
    try:
        shlex.split(chunk + _ESCAPE_PROBE_CHAR)
    except ValueError:
        return False
    return True


def _quote_state(char: str, quote: str) -> str:
    """The quote in force after `char`: "" outside quotes, or the quote character inside."""
    if quote:
        return "" if char == quote else quote
    return char if char in "'\"" else ""


def space_operators(line: str) -> str:
    """`line` with a space either side of each unquoted control operator.

    shlex then returns each operator as a token of its own. `;`, `|`, `||`, `|&`, `&`, `&&`,
    subshell parentheses, `$(` and backticks become boundaries; the `&` in a redirection (`>&`,
    `<&`, `&>`, `2>&1`) and the `|` in `>|` do not. A backslash-escaped character and quoted
    text are left alone, and a comment is dropped.

    A substitution inside double quotes still runs, but splitting there would break the quoted
    token, so it is not seen as a boundary.
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        following = line[index + 1] if index + 1 < len(line) else ""
        previous = line[index - 1] if index else ""
        if char == "\\" and quote != "'":
            out.append(line[index : index + 2])
            index += 2
            continue
        if quote or char in "'\"":
            quote = _quote_state(char, quote)
            out.append(char)
        elif char == "#" and (not previous or previous.isspace()):
            break  # a comment: the shell runs none of it
        elif char == "$" and following == "(":
            out.append(" ; ")
            index += 1
        elif char in _ALWAYS_SEPARATE:
            out.append(" ; ")
        elif char == "|" and previous != ">":
            pair = char + following
            if pair in {"||", "|&"}:
                index += 1
            out.append(f" {'||' if pair == '||' else '|'} ")
        elif char == "&" and previous not in _REDIRECT_CHARS and following != ">":
            if following == "&":
                index += 1
                out.append(" && ")
            else:
                out.append(" & ")
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _closing_paren(text: str, start: int) -> int:
    """The index of the `)` closing the `(` just before `start`, or len(text) if none does."""
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def quoted_substitutions(line: str) -> list[str]:
    """The bodies of the `$( )` and backtick substitutions inside double quotes in `line`.

    The shell runs these although they sit in a quoted word, which `space_operators` must leave
    whole; each body is returned so it can be tokenised as commands of its own. A substitution
    inside single quotes, or with its `$` or backtick escaped, is only text and is not returned.
    """
    bodies: list[str] = []
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == '"' and char == "$" and line[index + 1 : index + 2] == "(":
            end = _closing_paren(line, index + 2)
            bodies.append(line[index + 2 : end])
            index = end + 1
            continue
        if quote == '"' and char == "`":
            end = line.find("`", index + 1)
            end = len(line) if end == -1 else end
            bodies.append(line[index + 1 : end])
            index = end + 1
            continue
        if quote or char in "'\"":
            quote = _quote_state(char, quote)
        index += 1
    return bodies


def parse(command: str) -> Parse:
    """`command` tokenised with a synthetic `;` at each newline that ends a command.

    See the module docstring for why line-at-a-time tokenising, rather than handing shlex
    the whole string, is necessary at all. `truncated` is true when something never
    balanced -- the quote is genuinely unterminated -- in which case the remainder is
    dropped from `tokens` rather than being whitespace-split into words that were never
    really separate tokens.
    """
    tokens: list[str] = []
    pending = ""
    holding = False
    unread_heredocs: list[Heredoc] = []
    truncated_inside = False

    for line in command.split("\n"):
        if unread_heredocs:
            body_line = line.lstrip("\t") if unread_heredocs[0].strips_tabs else line
            if body_line == unread_heredocs[0].delimiter:
                unread_heredocs.pop(0)
            continue
        chunk = pending + line if holding else line
        try:
            line_tokens = shlex.split(space_operators(chunk))
        except ValueError:
            # The line stops inside something that continues below: drop a continuation's
            # backslash-newline the way a shell does, and keep a quoted value's newline.
            pending = chunk[:-1] if ends_mid_escape(chunk) else chunk + "\n"
            holding = True
            continue
        pending, holding = "", False
        unread_heredocs = heredocs_opened(line_tokens)
        if not line_tokens:
            continue
        if tokens:
            tokens.append(_SYNTHETIC_SEPARATOR)
        tokens.extend(line_tokens)
        for body in quoted_substitutions(chunk):
            inner = parse(body)
            truncated_inside = truncated_inside or inner.truncated
            if inner.tokens:
                tokens.append(_SYNTHETIC_SEPARATOR)
                tokens.extend(inner.tokens)

    return Parse(tokens=tokens, truncated=holding or truncated_inside)


def tokenise(command: str) -> list[str]:
    """`command` as tokens. Fail-open: an unparseable tail is silently dropped.

    Suits a guard that is a nudge rather than a boundary, where a false block (denying a
    command over a stray quoting mistake) costs more than a missed check. A guard that
    must not fail open should call `parse()` directly and act on `truncated` itself.
    """
    return parse(command).tokens


def command_segments(tokens: list[str]) -> list[list[str]]:
    """`tokens` split into per-command segments at bare `&&`/`||`/`;`/`|`/`&` tokens."""
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in COMMAND_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]
