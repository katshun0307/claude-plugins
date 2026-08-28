#!/usr/bin/env python3
"""外部に出る日本語の文章を, 機械判定できる規範だけで検査する hook.

対象は 2 つある.

- PreToolUse: Linear / esa / GitHub への書き込みと, gh コマンドでの PR / issue 本文
- Stop: そのターンで user に出したチャットの文章.
  文体は個人の規範なので, 環境変数 REVIEW_DRAFT_CHAT_STYLE=1 を置いた人にだけ効く

意味的なレビュー (素読み, 事実と推測の分離, 記載範囲) はここでは扱わない.
そちらは prompt hook と agent hook が受け持つ.

検査対象から除外するもの:
- code fence の中身 (log や設定の貼り付け)
- blockquote の中身 (他人の文章の引用)
- 4 space 以上の indent された行 (code block)
"""
import json
import os
import re
import sys

# チャットの文体検査は個人の規範なので, 明示的に有効にした人にだけ効かせる
CHAT_STYLE_ENV = "REVIEW_DRAFT_CHAT_STYLE"

RULES_DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "rules", "writing-rules.md")

MIN_LEN_TOOL = 20   # これより短い本文は検査しない
MIN_LEN_CHAT = 25   # チャットは短い応答が多いので閾値を上げる
MAX_BLOCKS = 3      # 同一 session で連続 deny する上限. 超えたら人の判断に委ねる
MAX_CHAT_BLOCKS = 1  # 1 ターンで deny する上限

TEXT_KEYS = ("title", "name", "description", "body", "body_md", "content")
PATCH_KEYS = ("new_string", "text")

GH_WRITE = re.compile(
    r"\bgh\s+(pr|issue|release)\s+(create|edit|comment|review|update)\b")

LLM_PHRASES = (
    "重要なのは", "本節では", "本記事では", "本章では", "まとめると", "要するに",
    "に他ならない", "正面から", "不可欠", "核心的", "鍵となる", "多角的",
    "包括的", "総合的", "掘り下げ", "深掘り", "言語化", "非常に", "極めて",
    "大いに", "と言えるだろう",
)

# チャットにも適用する文体の検査
STYLE_CHECKS = (
    ("6. 文体 / 常体で書く",
     re.compile(r"(です|ます|ました|でした|ません|ましょう)(?=[.。,、\s)）」]|$)")),
    ("6. 文体 / 句読点はカンマとピリオドを使う",
     re.compile(r"[、。]")),
    ("6. 文体 / ダッシュを地の文・見出しで使わない",
     re.compile(r"[—―]")),
)

# 成果物の本文だけに適用する検査
DRAFT_CHECKS = (
    ("1. 記載範囲 / 変更履歴を残さない",
     re.compile(r"~~.+?~~|修正済み|変更履歴|<details|※\s*当時")),
    ("2. 読者の前提 / ファイルパスを省略しない",
     re.compile(r"\.\.\.[/\\]|[/\\]\.\.\.")),
    ("11. Markdown の構造 / 表のセル内で改行しない",
     re.compile(r"^\s*\|.*<br\s*/?>", re.I)),
)

ADJACENT_LIST = re.compile(r"^([-*+]|\d+\.) .*\n\n(?=([-*+]|\d+\.) )", re.M)
LINEAR_URL = re.compile(r"linear\.app/[^/\s]+/issue/")

HEREDOC = re.compile(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?\s*\n(.*?)\n\s*\1\b",
                     re.S)
INLINE_BODY = re.compile(r"--(?:body|title)[= ]\s*(['\"])(.*?)\1", re.S)
BODY_FILE = re.compile(r"--body-file[= ]\s*(\S+)")


def collect_from_tool_input(tool_input):
    """検査対象のテキストを (ラベル, 本文) の list で返す."""
    segments = []
    for key in TEXT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            segments.append((key, value))
    patch = tool_input.get("patch")
    if isinstance(patch, list):
        for index, op in enumerate(patch):
            if not isinstance(op, dict):
                continue
            for key in PATCH_KEYS:
                value = op.get(key)
                if isinstance(value, str) and value.strip():
                    segments.append(("patch[%d].%s" % (index, key), value))
    return segments


def collect_from_bash(command):
    """gh コマンドから PR / issue の本文を取り出す."""
    if not GH_WRITE.search(command):
        return []
    segments = []
    for name, body in HEREDOC.findall(command):
        segments.append(("heredoc<%s>" % name, body))
    if not segments:
        for _, body in INLINE_BODY.findall(command):
            segments.append(("--body", body))
    for path in BODY_FILE.findall(command):
        path = path.strip("'\"")
        if path == "-":
            continue
        try:
            with open(os.path.expanduser(path)) as handle:
                segments.append(("--body-file %s" % path, handle.read()))
        except IOError:
            pass
    return segments


INLINE_CODE = re.compile(r"`[^`]*`")


def mask(text):
    """引用と code を空行に置き換える.

    対象は code fence, blockquote, indent された code block, inline code である.
    他人の文章や log の中の表記は規範の対象ではない.
    行番号を保つために行数は変えない.
    """
    out = []
    in_fence = False
    for line in text.split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            out.append("")
            continue
        if in_fence or re.match(r"^\s*>", line) or re.match(r"^\s{4,}\S", line):
            out.append("")
            continue
        out.append(INLINE_CODE.sub(" ", line))
    return "\n".join(out)


def inspect(label, text, tool_name, draft):
    findings = []
    masked = mask(text)
    originals = text.split("\n")
    checks = STYLE_CHECKS + (DRAFT_CHECKS if draft else ())
    for lineno, line in enumerate(masked.split("\n"), start=1):
        if not line.strip():
            continue
        shown = originals[lineno - 1].strip()
        for item, pattern in checks:
            hit = pattern.search(line)
            if hit:
                findings.append((item, label, lineno, shown, hit.group(0)))
        if line.count("・") >= 2:
            findings.append(("6. 文体 / 中黒を並列で使わない", label, lineno,
                             shown, "・"))
        for phrase in LLM_PHRASES:
            if phrase in line:
                findings.append(("10. LLM っぽい表現", label, lineno,
                                 shown, phrase))
        if draft and tool_name.endswith("save_issue"):
            hit = LINEAR_URL.search(line)
            if hit:
                findings.append(("既存 Issue の参照は識別子だけを書く", label, lineno,
                                 shown, hit.group(0)))
    if draft:
        for hit in ADJACENT_LIST.finditer(masked):
            lineno = masked[:hit.start()].count("\n") + 1
            findings.append(("11. Markdown の構造 / 隣接する list を空行だけで区切らない",
                             label, lineno, hit.group(0).split("\n")[0].strip(), ""))
    return findings


def state_path(key):
    tmp = os.environ.get("TMPDIR", "/tmp")
    directory = os.path.join(tmp, "claude-review-draft-gate")
    try:
        os.makedirs(directory)
    except OSError:
        pass
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", key or "no-key")
    return os.path.join(directory, safe + ".count")


def read_count(path):
    try:
        with open(path) as handle:
            return int(handle.read().strip() or "0")
    except (IOError, ValueError):
        return 0


def write_count(path, count):
    try:
        with open(path, "w") as handle:
            handle.write(str(count))
    except IOError:
        pass


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.exit(0)


def build_reason(findings, header):
    grouped = {}
    for item, label, lineno, line, hit in findings:
        grouped.setdefault(item, []).append((label, lineno, line, hit))

    lines = [header]
    for item in sorted(grouped):
        lines.append("")
        lines.append("## " + item)
        for label, lineno, line, hit in grouped[item][:5]:
            excerpt = line if len(line) <= 120 else line[:120] + " ..."
            suffix = " (該当: %s)" % hit if hit else ""
            lines.append("- %s L%d: %s%s" % (label, lineno, excerpt, suffix))
        if len(grouped[item]) > 5:
            lines.append("- 他 %d 件" % (len(grouped[item]) - 5))
    lines.append("")
    lines.append("項目名は %s の見出しに対応する." % RULES_DOC)
    return "\n".join(lines)


def handle_stop(event):
    if os.environ.get(CHAT_STYLE_ENV) not in ("1", "true", "yes"):
        sys.exit(0)

    message = event.get("last_assistant_message")
    if not isinstance(message, str) or len(message.strip()) < MIN_LEN_CHAT:
        sys.exit(0)

    findings = inspect("chat", message, "", draft=False)
    if not findings:
        sys.exit(0)

    path = state_path("chat-" + (event.get("prompt_id") or
                                 event.get("session_id") or ""))
    count = read_count(path) + 1
    write_count(path, count)
    if count > MAX_CHAT_BLOCKS:
        sys.exit(0)

    reason = build_reason(
        findings,
        "直前のチャット出力が文体の規範に反している. 同じ内容を規範に沿って書き直して user に出し直せ.")
    emit({"decision": "block", "reason": reason})


def handle_pre_tool_use(event):
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        sys.exit(0)

    if tool_name == "Bash":
        command = tool_input.get("command")
        segments = collect_from_bash(command) if isinstance(command, str) else []
    else:
        segments = collect_from_tool_input(tool_input)

    if sum(len(text) for _, text in segments) < MIN_LEN_TOOL:
        sys.exit(0)

    findings = []
    for label, text in segments:
        findings.extend(inspect(label, text, tool_name, draft=True))

    path = state_path(event.get("session_id", ""))
    if not findings:
        write_count(path, 0)
        sys.exit(0)

    count = read_count(path) + 1
    write_count(path, count)
    reason = build_reason(
        findings,
        "書き込み前の機械チェックで規範違反を検出した. 該当箇所を直してから再度書き込むこと.\n"
        "これは機械判定だけである. 素読みや事実と推測の分離は検査していない.")

    if count > MAX_BLOCKS:
        write_count(path, 0)
        emit({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason":
                    "機械チェックが %d 回連続で違反を検出したので, 判断を委ねる.\n\n%s"
                    % (MAX_BLOCKS, reason),
            }
        })

    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    })


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)

    if event.get("hook_event_name") == "Stop":
        handle_stop(event)
    else:
        handle_pre_tool_use(event)


if __name__ == "__main__":
    main()
