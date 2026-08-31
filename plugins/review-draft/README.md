# review-draft

Linear / esa / GitHub に投稿する日本語の文章を, 書き込みが実行された直後に規範と照合する hook.

自分で書いた文章を自分で読み返すと, 会話で共有済みの前提を補って読んでしまう. hook の判定役は会話を一切持たないので, issue や PR を初めて開く読者と同じ条件で読む.

## 構成

3 層に分かれている.

| 層 | 実装 | 実測 | 見るもの |
| --- | --- | --- | --- |
| 機械チェック | command hook (python) | 40ms | 文体, Markdown の構造, 変更履歴, パス省略 |
| 素読み | prompt hook (haiku) | 4s | 文脈ゼロの読者としてつまずいた箇所 |
| 規範照合 | prompt hook (haiku) | 5s | 規範との突き合わせ |

prompt hook は並列に走るので, 待ち時間は遅い方の 1 本ぶんで済む.

3 層とも PostToolUse に置いてある. 違反があっても書き込みは止まらず, 指摘だけが Claude に返る. 反映して書き直すか, 見送って user に伝えるかは Claude が決める.

## 書き込みを止めずに指摘を返す

検査は PostToolUse に置いてある. 投稿を止める経路が無いので, 指摘が的外れでも作業は止まらない.

- 機械チェックは `hookSpecificOutput.additionalContext` で指摘を返す. エラーではなく素の文脈として届く
- prompt hook は `ok: false` を返すと `decision: "block"` になる. tool は走り終えているので書き込みは止まらないが, `continueOnBlock: true` を付けないとターンが終わる

指摘だけを返すと「user に報告して終わる合図」と読まれるので, 選べることを明示する文を必ず添える. 反映する価値があると判断したら同じ tool で書き直す, 見送るならその判断を user に一言添える, のどちらかである.

prompt hook の指摘が毎回変わると収束しないので, 挙げてよい指摘の水準も prompt に固定してある. 逐語引用を添えられること, 直さなければ読者が誤解すること, 1 度に 2 件までである.

## 発火する場所

- Linear の issue 本文, コメント, document
- esa の記事作成, 更新, コメント
- GitHub MCP の issue と PR の書き込み
- `gh pr create`, `gh pr edit`, `gh pr comment`, `gh pr review`, `gh issue create`, `gh issue edit`, `gh issue comment`
- チャット出力 (機械チェックのみ. 既定では無効)

`gh` は heredoc, `--body`, `--body-file` のどれからでも本文を取り出す. ただし `--body-file` の中身を読めるのは機械チェックだけで, prompt hook は Read を持たないので検査しない.

Bash の絞り込みに `if` は使っていない. 理由は後述する. 代わりに prompt の中で「gh の書き込みコマンドの本文以外は必ず ok を返す」と指示している.

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

## 規範を直す

規範の単一の出所は `rules/semantic-rules.md` である. prompt hook は Read を持たないので, 規範は生成時に prompt へ埋め込む.

```bash
/usr/bin/python3 plugins/review-draft/scripts/sync-hooks.py
```

`hooks/hooks.json` が再生成されるので, rules と一緒に commit する. `hooks/hooks.json` を手で編集しても次の生成で消える.

`rules/writing-rules.md` は機械チェックが返す項目名の参照先である. 判定そのものは `hooks/review-draft-gate.py` の正規表現が持つ.

## 暴走の防ぎ方

- 書き込みそのものを止める経路が無い. 指摘が的外れでも投稿は通る
- Stop の文体チェックは prompt_id ごとに 1 回しか差し戻さない
- code fence, blockquote, inline code, indent された block は検査対象から外す. log や引用が引っかからない

## hook の実測仕様

公式 docs と食い違う箇所がある. どれも silent に no-op するので気付きにくい.

- `model` に `haiku` のような alias を書くと 404 になる. `claude-haiku-4-5-20251001` と完全な id を書く
- prompt hook と agent hook が返すのは `{"ok": bool, "reason": str}` である. `hookSpecificOutput.permissionDecision` は解釈されない
- prompt hook の `ok: false` は必ず `decision: "block"` になる. PreToolUse に置く限り書き込みは deny される. `continueOnBlock` が制御するのはターンを続けるかどうかだけで, tool の deny は取り消せない
- PostToolUse の入力 JSON には `tool_response` が入る. Bash では実行結果ごと prompt hook に渡るので, 検査対象外だと prompt 側で明示する
- Stop hook は `{"decision": "block", "reason": "..."}` を返す
- `if` は permission rule を 1 つしか解釈しない. `|` で連結すると常に不一致になる
- `if` は `&&` を含むコマンドを絞り込めない. `cd /tmp && echo done` は `Bash(gh pr create:*)` を含む全ての rule に「条件成立」と判定される. rule ごとに hook を分けて置くと, 1 コマンドにつき hook がその本数だけ走り, コマンドに混じった日本語が下書きとして deny される. Bash の絞り込みは prompt 側で行う

`if` の判定は headless で確かめられる.

```bash
claude -p 'Run exactly this bash command, nothing else: cd /tmp && echo done' \
  --debug hooks --debug-file ./probe.log
grep -c 'Skipping hook due to if condition' probe.log
```
