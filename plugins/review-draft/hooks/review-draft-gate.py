#!/usr/bin/env python3
"""外部に出る日本語の文章を, 機械判定できる規範だけで検査する hook.

対象は 2 つある.

- PostToolUse: Linear / esa / GitHub への書き込みと, gh コマンドでの PR / issue 本文
- Stop: そのターンで user に出したチャットの文章.
  文体は個人の規範なので, 環境変数 REVIEW_DRAFT_CHAT_STYLE=1 を置いた人にだけ効く

意味的なレビュー (素読み, 事実と推測の分離, 記載範囲) はここでは扱わない.
そちらは prompt hook と agent hook が受け持つ.

書き込みは止めない. PreToolUse で deny すると, 直せる指摘のために投稿が毎回止まり,
user に判断が投げ返される. 書き込みが通ったあとに指摘だけを additionalContext で返し,
書き直すかどうかは Claude に決めさせる.

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
MAX_CHAT_FEEDBACKS = 1  # 1 ターンでチャット文体を差し戻す上限

# 指摘に必ず添える. 指摘だけを返すと「user に報告して終わる合図」に読まれるので,
# 反映するかを Claude が選べることと, 見送るときの振る舞いを書いておく
CHOICE = ("反映する価値があると判断したら, 同じ tool で本文を書き直せ."
          " user への確認は要らない. 見送るならその判断を user に一言添えよ.")

TEXT_KEYS = ("title", "name", "description", "body", "body_md", "content")
PATCH_KEYS = ("new_string", "text")

# コマンド片の先頭に来たときだけ書き込みとみなす. 引用符の中やテストデータに
# 現れた同じ字面を拾わないよう, 前置きに許すのは環境変数の代入だけである.
GH_WRITE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"gh\s+(?:pr|issue|release)\s+(?:create|edit|comment|review|update)\b")

# コマンド片の切れ目. 引用符の外に現れたものだけを切れ目として扱う
SEPARATORS = ("&&", "||", ";", "|", "\n", "&")

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


def mask_heredocs(command):
    """heredoc の中身を空白で潰した文字列と, (名前, 中身, << の位置) を返す.

    中身を潰しておかないと, コマンド片を切り分けるときに heredoc の中の
    セミコロンやパイプを切れ目と誤読する.
    """
    masked = list(command)
    found = []
    for hit in HEREDOC.finditer(command):
        found.append((hit.group(1), hit.group(2), hit.start()))
        for index in range(hit.start(2), hit.end(2)):
            masked[index] = " "
    return "".join(masked), found


def split_commands(masked):
    """引用符の外の区切りでコマンド片に分け, (開始, 終了) の list を返す."""
    spans = []
    start = 0
    quote = None
    index = 0
    while index < len(masked):
        char = masked[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        for sep in SEPARATORS:
            if masked.startswith(sep, index):
                spans.append((start, index))
                index += len(sep)
                start = index
                break
        else:
            index += 1
    spans.append((start, len(masked)))
    return [(a, b) for a, b in spans if masked[a:b].strip()]


def collect_from_bash(command):
    """gh の書き込みコマンドから PR / issue の本文を取り出す.

    本文とみなすのは, 書き込みコマンドの片の中にあるものだけである. 同じコマンド行に
    別の heredoc やテストデータが並んでいても混ぜない.
    """
    masked, heredocs = mask_heredocs(command)
    segments = []
    for start, end in split_commands(masked):
        piece = masked[start:end].lstrip()
        if not GH_WRITE.match(piece):
            continue
        inside = [(name, body) for name, body, at in heredocs if start <= at < end]
        for name, body in inside:
            segments.append(("heredoc<%s>" % name, body))
        # heredoc がある片では --body の値が $(cat <<EOF ...) になる. 二重に取らない
        if not inside:
            for _, body in INLINE_BODY.findall(masked[start:end]):
                segments.append(("--body", body))
        for path in BODY_FILE.findall(masked[start:end]):
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


def state_path(key, suffix=".count"):
    tmp = os.environ.get("TMPDIR", "/tmp")
    directory = os.path.join(tmp, "claude-review-draft-gate")
    try:
        os.makedirs(directory)
    except OSError:
        pass
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", key or "no-key")
    return os.path.join(directory, safe + suffix)


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


def handle_post_tool_use(event):
    """書き込みが通った本文を検査して, 指摘を additionalContext で返す."""
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
    if not findings:
        sys.exit(0)

    reason = build_reason(
        findings,
        "書き込んだ本文の機械チェックで規範違反を検出した.\n"
        + CHOICE + "\n"
        "これは機械判定だけである. 素読みや事実と推測の分離は検査していない.")
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": reason,
        }
    })


def handle_chat_style(event):
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
    if count > MAX_CHAT_FEEDBACKS:
        sys.exit(0)

    reason = build_reason(
        findings,
        "直前のチャット出力が文体の規範に反している. 同じ内容を規範に沿って書き直して user に出し直せ.")
    emit({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": reason,
        }
    })


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)

    name = event.get("hook_event_name")
    if name == "Stop":
        handle_chat_style(event)
    else:
        handle_post_tool_use(event)


if __name__ == "__main__":
    main()
