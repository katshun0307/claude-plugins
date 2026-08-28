#!/usr/bin/env python3
"""rules/semantic-rules.md から hooks/hooks.json を組み立てる.

規範の単一の出所は rules/semantic-rules.md である. prompt hook は Read を持たないので,
規範は prompt に埋め込む. semantic-rules.md を編集したらこれを実行して commit する.

    /usr/bin/python3 plugins/review-draft/scripts/sync-hooks.py

hook は 3 層ある.

- 機械チェック (command hook, 40ms) 文体と Markdown の構造
- 素読み (prompt hook, 4s) 文脈ゼロの読者として通読し, つまずいた箇所を返す
- 規範照合 (prompt hook, 5s) 埋め込んだ規範と本文を突き合わせる

prompt hook は同じ group 内で並列に走るので, 待ち時間は遅い方の 1 本ぶんで済む.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(ROOT, "rules", "semantic-rules.md")
HOOKS = os.path.join(ROOT, "hooks", "hooks.json")
GATE = "${CLAUDE_PLUGIN_ROOT}/hooks/review-draft-gate.py"

MODEL = "claude-haiku-4-5-20251001"  # alias ("haiku") は 404 になるので完全な id を書く

# 本文を書き込む tool
WRITE_TOOLS = (
    "mcp__linear-server__(save_issue|save_comment|save_document)"
    "|mcp__(claude_ai_)?esa__esa_(create_post|update_post|create_comment"
    "|update_comment|append_post|prepend_post)"
    "|mcp__github__(issue_write|pull_request_write|create_issue|update_issue"
    "|add_issue_comment|create_pull_request|update_pull_request"
    "|add_comment_to_pending_review|create_and_submit_pull_request_review)"
)

# if は permission rule を 1 つしか解釈しない. | で連結すると常に不一致になる.
GH_WRITE_RULES = (
    "Bash(gh pr create:*)",
    "Bash(gh pr edit:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh pr review:*)",
    "Bash(gh issue create:*)",
    "Bash(gh issue edit:*)",
    "Bash(gh issue comment:*)",
)

# prompt hook / agent hook は hookSpecificOutput ではなく ok / reason を返す
OUTPUT = (
    "出力は JSON だけにせよ. 前置きも説明も code fence も付けるな.\n"
    "問題が無い: {\"ok\": true}\n"
    "問題がある: {\"ok\": false, \"reason\": \"<指摘>\"}\n"
    "reason は日本語で書け."
)

SCOPE = (
    "tool_input に含まれる日本語の本文だけを対象にする. 本文とは title / name /"
    " description / body / body_md / content, patch の new_string, Bash なら"
    " gh コマンドの --body に埋まった文章である.\n\n"
    "次のときは検査せず {\"ok\": true} を返せ.\n"
    "- 本文が含まれていない呼び出し (state / label / assignee / priority / cycle の変更だけ)\n"
    "- 本文が 200 文字未満\n"
    "- 本文がここに含まれず --body-file のようなファイル参照になっている\n"
    "- patch で差分だけが渡された場合は, 差分に現れた記述だけを対象にする\n"
)

READER_PROMPT = (
    "$ARGUMENTS は Claude Code の PreToolUse hook の入力 JSON である.\n"
    + SCOPE +
    "\nあなたはこの文章を今初めて開いた開発チームのメンバーである."
    " 書かれた経緯も, これに至った会話も一切知らない."
    " その立場で通読し, 次のどれかに当てはまる箇所があるときだけ ok: false を返せ.\n"
    "1. 前提が書かれておらず, 何を指しているかが読者に決まらない\n"
    "2. なぜこの文章が書かれたのか, 読者に何を求めているのかが読み取れない\n"
    "3. 本文の中だけで閉じる矛盾がある (表の値と地の文の値が違う, 前提と結論が食い違う)\n"
    "4. 数値や固有名に単位や出典が無く, 規模が読み取れない\n\n"
    "言い回しの好み, 文体, Markdown の構造は指摘するな. 別の hook が検査済みである."
    " 外部の事実が間違っているという断定もするな. 元データを持たないので判定できない."
    " 迷ったら ok: true を返せ.\n\n"
    "reason には原文からの引用と, 読者としてつまずいた理由を, 重いものから 3 件まで書け."
    " 直し方は書かなくてよい.\n\n"
    + OUTPUT
)


def rules_prompt():
    with open(RULES) as handle:
        rules = handle.read().strip()
    return (
        "$ARGUMENTS は Claude Code の PreToolUse hook の入力 JSON である.\n"
        + SCOPE +
        "\n本文を次の規範と照合せよ.\n\n"
        "<規範>\n" + rules + "\n</規範>\n\n"
        "規範違反があるときだけ ok: false を返せ. reason には原文からの引用, 違反した規範の"
        "項目名, 具体的な直し方を, 重いものから 3 件まで書け.\n"
        "規範に項目が無い好みの問題は挙げるな. 会話の経緯は与えられていないので,"
        " 本文だけで判断せよ. 迷ったら ok: true を返せ.\n\n"
        + OUTPUT
    )


def gate_hook(status):
    return {
        "type": "command",
        "command": "/usr/bin/python3",
        "args": [GATE],
        "statusMessage": status,
        "timeout": 15,
    }


def prompt_hook(prompt, status, extra=None):
    hook = {
        "type": "prompt",
        "prompt": prompt,
        "model": MODEL,
        "statusMessage": status,
        "timeout": 25,
    }
    if extra:
        hook.update(extra)
    return hook


def main():
    reader = READER_PROMPT
    checker = rules_prompt()

    config = {
        "description": "投稿前の下書きと, チャット出力を規範と照合する",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": WRITE_TOOLS,
                    "hooks": [
                        gate_hook("下書きの機械チェック"),
                        prompt_hook(reader, "文脈ゼロの素読み"),
                        prompt_hook(checker, "規範との照合"),
                    ],
                },
                {
                    "matcher": "Bash",
                    "hooks": [gate_hook("下書きの機械チェック")] + [
                        prompt_hook(prompt, status, {"if": rule})
                        for rule in GH_WRITE_RULES
                        for prompt, status in ((reader, "文脈ゼロの素読み"),
                                               (checker, "規範との照合"))
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [gate_hook("チャット文体の機械チェック")],
                }
            ],
        },
    }

    with open(HOOKS, "w") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("updated %s" % HOOKS)


if __name__ == "__main__":
    main()
