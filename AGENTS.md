# AGENTS.md

## 目的

自宅基盤（NAS・ネットワーク・バックアップ・常駐サービス・統制データ基盤）の設計書・決定記録（ADR）・構成図・IaC / Docker スタックを公開リファレンスとしてまとめたサニタイズ版リポジトリ。

## 正本と生成物

- 正本（公開版）：
  - `docs/*.md`（設計・方針ドキュメント。1ドキュメント1トピック）
  - `docs/decisions/NNNN-<slug>.md`（連番の意思決定記録）
  - `docs/diagrams/<name>.html`（[`caru-ini/stack-diagram`](https://github.com/caru-ini/stack-diagram) で作成した自己完結 HTML の構成図）
  - `README.md`（目次と全体像・構成サマリ。詳細は各ドキュメントに書く）
  - `stacks/`・`ansible/`（プレースホルダー統一済みの構成コード）
- 生成物：
  - `docs/diagrams/<name>.png`（`docs/diagrams/<name>.html` から headless Chromium で生成するライト版の構成図画像）

## スキーマ

- 各ドキュメントの先頭に状態を明記する：`状態： 設計` / `状態： 構築済み` / `状態： 運用中`（方針文書は `状態： 方針`）。
- 決定の記録は `docs/decisions/NNNN-<slug>.md` の連番命名規則とし、実作業日時は記載しない。採用した案だけでなく落とした案とその理由も残す。
- 構成図を書く際は [`caru-ini/stack-diagram`](https://github.com/caru-ini/stack-diagram) を使い、`docs/diagrams/` に HTML とライト PNG のみ（`render_png.py --single`）を出力して Markdown へ埋め込む。生成された `docs/diagrams/*.html` には巨大な `data:font/woff2;base64` 行が埋め込まれているため、ファイルビューアで先頭から丸読みせず、スクリプト等で base64 行を除外して参照・編集する。
- 1ドキュメント1トピックを原則とする。

## 禁止事項

- 秘密情報（パスワード・API キー・トークンの値）の記録（どこに保管しているかだけを書く）
- 個人情報・実ドメイン・実 Tailscale IP・実 LAN サブネット・実機固有型番／シリアル・他プライベートリポジトリ名・実作業日時・実機監査ログ・実機スクリーンショットの混入
- `scripts/check_sanitized.py` および CI（`gitleaks` 含む）を通過しない状態での commit & push
- 設計ドキュメントを「構築済み・運用中」の現実として読み違える記述、または設計と現況が食い違った状態の放置
- `README.md` への詳細設計の書き込み（詳細は `docs/` に置く）
