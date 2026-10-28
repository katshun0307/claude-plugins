# review-draft

Linear / esa / GitHub に投稿する日本語の文章を, 書き込みが実行された直後に規範と照合する hook.

自分で書いた文章を自分で読み返すと, 会話で共有済みの前提を補って読んでしまう. hook の判定役は会話を一切持たないので, issue や PR を初めて開く読者と同じ条件で読む.

## 構成

3 層に分かれている.

| 層 | 実装 | 見るもの | 載る場所 |
| --- | --- | --- | --- |
| 機械チェック | command hook (python) | 文体, Markdown の構造, 変更履歴, パス省略 | MCP と Bash |
| 素読み | prompt hook (haiku) | 文脈ゼロの読者としてつまずいた箇所 | MCP のみ |
| 規範照合 | agent hook (haiku) | `rules/semantic-rules.md` を読んでの突き合わせ | MCP のみ |

3 本は並列に走るので, 待ち時間は遅い方の 1 本ぶんで済む.

3 層とも PostToolUse に置いてある. 違反があっても書き込みは止まらず, 指摘だけが Claude に返る. 反映して書き直すか, 見送って user に伝えるかは Claude が決める.

## 書き込みを止めずに指摘を返す

検査は PostToolUse に置いてある. 投稿を止める経路が無いので, 指摘が的外れでも作業は止まらない.

- 機械チェックは `hookSpecificOutput.additionalContext` で指摘を返す. エラーではなく素の文脈として届く
- prompt hook は `ok: false` を返すと `decision: "block"` になる. tool は走り終えているので書き込みは止まらないが, `continueOnBlock: true` を付けないとターンが終わる

指摘だけを返すと「user に報告して終わる合図」と読まれるので, 選べることを明示する文を必ず添える. 反映する価値があると判断したら同じ tool で書き直す, 見送るならその判断を user に一言添える, のどちらかである.

prompt hook の指摘が毎回変わると収束しないので, 挙げてよい指摘の水準も prompt に固定してある. 逐語引用を添えられること, 直さなければ読者が誤解すること, 1 度に 2 件までである.

## 規範照合を agent hook にしてある理由

Claude Code は hook が返す指摘に prompt 全文を添える. prompt hook は `from command: "<prompt>"` と `[<prompt>]:` の 2 箇所に写すので, 規範を埋め込んだ 3130 文字の prompt が指摘 1 件につき 2 回コンテキストへ入っていた. agent hook は Read を持つので規範を埋め込まずに済み, 写る箇所も 1 箇所である.

実測はこうなった.

| 条件 | prompt hook | agent hook |
| --- | --- | --- |
| 対象外の Bash 呼び出し | 3.20s | 2.89s |
| 本文 140-180 文字, 指摘あり | 10.02s | 26-30s |
| 指摘 1 件あたりの写り込み | 約 6.3KB | 約 1.3KB |

対象外と判定するときは agent のほうがわずかに速い. tool を使わず 1 往復で返し, prompt が短い分だけ読む量も少ないためである. 遅くなるのは実際に書き込んだときだけで, 書き込みは止まらないので待ち時間が作業を詰まらせることは無い.

指摘の質も上がる. 規範ファイルを読むので, 違反した項目を見出し番号ごと引用してくる.

## 発火する場所

- Linear の issue 本文, コメント, document
- esa の記事作成, 更新, コメント
- GitHub MCP の issue と PR の書き込み
- `gh pr create`, `gh pr edit`, `gh pr comment`, `gh pr review`, `gh issue create`, `gh issue edit`, `gh issue comment`
- チャット出力 (機械チェックのみ. 既定では無効)

`gh` 経由の書き込みは機械チェックだけを通る. heredoc, `--body`, `--body-file` のどれからでも本文を取り出す.

## Bash に LLM の層を載せない

PostToolUse の入力 JSON には `tool_response` が入る. `gh pr view` の出力は PR 本文そのものなので, Bash に prompt hook や agent hook を載せると, 読み取り専用のコマンドの出力を下書きとして読んで誤指摘を返す. prompt に「`tool_response` は検査するな」と書いても指示にすぎず守られない (実測で再現した).

`if` による絞り込みでは解けない. 書き込みコマンドは 7 通りあり, `if` は permission rule を 1 つしか解釈しないので hook を 7 本に割ることになる. LLM の層が 2 本あるので 1 コマンドにつき 14 本走る. `if` は `&&` を含むコマンドを絞り込めず全ての rule に一致するため, これは書き込みとは無関係なコマンドでも起きる. 待ち時間と API 呼び出しがその本数だけ積む.

そのため Bash には機械チェックだけを置いてある. `gh pr create` の本文に素読みと規範照合をかけたいなら, MCP の GitHub tool から書き込む.

機械チェックが本文とみなすのは, gh の書き込みコマンドの片の中にあるものだけである. コマンド行に無関係な heredoc が並んでいても, 引用符の中に同じ字面があっても混ぜない.

## 導入

```bash
claude plugin marketplace add katshun0307/claude-plugins
claude plugin install review-draft@katshun0307
```

ローカルの clone から入れる場合は marketplace の path を渡す.

```bash
claude plugin marketplace add ~/ghq/github.com/katshun0307/claude-plugins
```

## チャット出力の文体検査

チャットの文体 (常体, カンマとピリオド) は個人の規範なので既定では無効にしてある. 有効にするなら `~/.claude/settings.json` に環境変数を置く.

```json
{
  "env": {
    "REVIEW_DRAFT_CHAT_STYLE": "1"
  }
}
```

有効にすると, Claude の応答が文体の規範に反したときに 1 ターンにつき 1 回だけ書き直させる.

## チャット出力の issue リンク検査

チャットで issue 識別子 (`SCR-123` など) をリンクにせず書いたら書き直させる. 識別子の prefix はチームごとに違うので, 既定では無効にしてある. 有効にするなら prefix をカンマ区切りで置く. `REVIEW_DRAFT_ISSUE_URL` を置くと, 指摘に正しいリンクの書式が添えられる.

```json
{
  "env": {
    "REVIEW_DRAFT_ISSUE_PREFIXES": "SCR,MNT,QA",
    "REVIEW_DRAFT_ISSUE_URL": "https://linear.app/omni-hub/issue/{id}"
  }
}
```

Markdown リンクの中, 素の URL の中, inline code, code fence, 引用の中の識別子は対象外である. 文体検査と同じく, 書き直させるのは 1 ターンにつき 1 回までである.

## 規範を直す

規範の単一の出所は `rules/semantic-rules.md` である. agent hook が実行時に Read するので, 規範を直しただけなら再生成は要らない.

`hooks/hooks.json` を直すときだけ生成し直す.

```bash
/usr/bin/python3 plugins/review-draft/scripts/sync-hooks.py
```

`hooks/hooks.json` を手で編集しても次の生成で消える.

`rules/writing-rules.md` は機械チェックが返す項目名の参照先である. 判定そのものは `hooks/review-draft-gate.py` の正規表現が持つ.

## 暴走の防ぎ方

- 書き込みそのものを止める経路が無い. 指摘が的外れでも投稿は通る
- Bash では LLM を呼ばない. 読み取り専用のコマンドで haiku が走ることは無い
- Stop の文体チェックは prompt_id ごとに 1 回しか差し戻さない
- code fence, blockquote, inline code, indent された block は検査対象から外す. log や引用が引っかからない

## hook の実測仕様

公式 docs と食い違う箇所がある. どれも silent に no-op するので気付きにくい.

- `model` に `haiku` のような alias を書くと 404 になる. `claude-haiku-4-5-20251001` と完全な id を書く
- prompt hook と agent hook が返すのは `{"ok": bool, "reason": str}` である. `hookSpecificOutput.permissionDecision` は解釈されない
- agent hook が使えるのは tool event (PreToolUse, PostToolUse, PermissionRequest) だけである. model の既定は Haiku, timeout の既定は 60s
- agent hook の prompt では `${CLAUDE_PLUGIN_ROOT}` が解決される. plugin 内のファイルを Read させられる
- prompt hook の `ok: false` は必ず `decision: "block"` になる. PreToolUse に置く限り書き込みは deny される. `continueOnBlock` が制御するのはターンを続けるかどうかだけで, tool の deny は取り消せない
- PostToolUse の入力 JSON には `tool_response` が入る. LLM の層に渡すと実行結果まで検査してしまうので, 載せる matcher を選ぶ
- Stop hook は `{"decision": "block", "reason": "..."}` を返す
- `if` は permission rule を 1 つしか解釈しない. `|` で連結すると常に不一致になる
- `if` は `&&` を含むコマンドを絞り込めない. `cd /tmp && echo done` は `Bash(gh pr create:*)` を含む全ての rule に「条件成立」と判定される. rule ごとに hook を分けて置くと, 1 コマンドにつき hook がその本数だけ走り, コマンドに混じった日本語が下書きとして deny される. Bash の絞り込みは prompt 側で行う

`if` の判定は headless で確かめられる.

```bash
claude -p 'Run exactly this bash command, nothing else: cd /tmp && echo done' \
  --debug hooks --debug-file ./probe.log
grep -c 'Skipping hook due to if condition' probe.log
```
