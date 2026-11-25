# 隔離された常駐 AI エージェントのランタイムに opencode serve / opencode run を選んだ

状態： 構築済み（Step 1〜3 完了：`ubuntu-desktop` で OpenCode 1.19.6 隔離コンテナ・対話利用・読み取り専用定時ジョブ 3 本稼働中・Issue #5）

## 決定

`ubuntu-desktop` 上で構築・検証する隔離された常駐 AI エージェント実行基盤のランタイムとして、**OpenCode（`opencode serve` / `opencode run`）** を隔離 Docker コンテナで採用する。

- **ランタイム**：OpenCode（Docker 隔離コンテナ、バージョン固定・自動更新無効）
  - 対話利用：`opencode serve`（Tailscale 閉域・認証必須）へ `opencode attach` 等で接続
  - 定時バッチ・自動化：`opencode run --format json` をホスト側タイマー（systemd timer 等）または薄いラッパーから起動
- **LLM プロバイダー（2 系統）**：
  - **主系**：**さくらの AI Engine**（OpenAI 互換 `https://api.ai.sakura.ad.jp/v1`、モデル例：`preview/DeepSeek-V4.1-Flash-1017` 等。月 3,000 リクエスト無償枠・国内完結・学習なし / ZDR）
  - **予備**：**OpenCode Console（旧 Zen）のクライアント向け無料枠**（`opencode/deepseek-v4.1-flash-free`・`opencode/glm-5.3-flash-free` 等）
- **配置**：Phase 1 先行構築として `ubuntu-desktop` 上の隔離 Docker コンテナへ配置し、Zabbix・NetBox・Fleet Free・Authentik とはネットワーク・ボリューム・秘密情報を完全に分離する（詳細設計は [隔離 AI エージェント実行基盤の設計（OpenCode）](../agent-runtime-design.md) を参照）

## 要件

- `ubuntu-desktop` 上で稼働中の 4 基盤（Zabbix・NetBox・Fleet Free・Authentik）と安全に同居でき、メモリ圧迫（OOM）や権限逸脱が他基盤へ波及しないこと
- クラウド LLM API の無料枠・国内 ZDR モデルを活用しつつ、パブリックプレビュー終了やレート制限時にモデルをすぐ差し替えられること
- リポジトリ調査・コード補助・Ansible 下書き・監視読み取り連携・夜間レポート生成など、将来の用途拡大に対応できる自由度があること
- 非 root・`/var/run/docker.sock` 非マウント・最小権限トークン・ツール実行権限ルール（`opencode.json`）でガードレールを明文化できること

## 理由

- **モデル選択肢の広さと逃げ道**：OpenCode 本体をランタイムにすることで、さくらの AI Engine（OpenAI 互換・1M コンテキスト・月 3,000 回と上限明示）を主系としつつ、OpenCode 公式クライアント限定の無料モデル群も予備として同一ランタイム内で切り替えられる
- **ファイル・git・シェル・MCP を含む作業自由度**：読み書き・検索・`git`・シェル実行・MCP 連携が標準で揃っているため、単純なチャット要約だけでなくリポジトリ調査や設定検証・構成自動化補助まで 1 基盤で扱える
- **対話とバッチの両立**：`opencode serve` への対話接続（`opencode attach`）と、夜間バッチ向けのワンショット実行（`opencode run --format json`）を同じ環境・同じ権限モデルで運用できる
- **既存ホスト資源に収まる**：`ubuntu-desktop` の実測空きメモリは `6.5 GiB`・CPU idle 95〜98%であり、`mem_limit: 1g` の隔離コンテナを 1 本追加しても十分な余裕がある

## 落とした案

- **OpenCode Console（旧 Zen）を外部エージェント用の無料プロバイダー単体として使う案**：無料枠は公式 OpenCode クライアント（CLI / Desktop / IDE）専用に制限されており、ZeroClaw や独自スクリプト等の外部クライアントから直接 API を叩くと `403 FreeTierError` で拒否されるため、単体プロバイダーとしての採用は見送った（OpenCode 本体をランタイムに採用することでこの制約を回避）
- **`ZeroClaw`（Rust）**：メモリ消費数 MB〜数十 MB・非 root（UID `65534`）前提で隔離しやすく、Discord Bot・Cron・Webhook・MCP が単体で揃う有力候補だった。今回はリポジトリ操作やコード補助まで見据えた作業自由度と OpenCode 無料枠の併用を優先して見送った（将来 Discord 常駐特化の超軽量ボットが必要になった場合の最有力候補として残す）
- **`OpenClaw`（Node.js）**：さくらの AI Engine + Discord 連携の公式記事やエコシステムが厚い一方、ZeroClaw よりメモリ消費が大きく、Heartbeat 等の自律ポーリングを回すとさくらの AI Engine 無料枠（月 3,000 リクエスト）を枯渇させやすいため見送った
- **`PydanticAI` / OpenAI Agents SDK による Python 自作コンテナ**：Zabbix アラート要約や定型バッチの制御性（429 リトライ・回数制限・最小権限）では最も堅いが、汎用的な対話・リポジトリ操作・ツール拡張を自前で保守する負担が増えるため主軸にはしなかった（定時起動や Discord Webhook 送信などの外側ラッパーだけ薄く Python / Shell で補う）
- **`n8n`**：GUI で Webhook・Discord・GitHub を繋ぎやすいが、常駐メモリ消費が 500 MB〜1 GB 規模と大きく、コード・git 操作中心の用途とズレるため見送った
- **ローカル LLM 推論（Ollama / vLLM 等）**：`ubuntu-desktop` は外付け GPU なし・搭載メモリ 12 GB（実効 11.6 GiB）の制約があり、4 基盤と同居させるには無理があるため対象外とした（ローカル推論を試す場合は GPU を積む `workstation-01` 側で別途検討する）

## 注意

- **デフォルト権限の強さに対するガードレールが必須**：OpenCode はシェル・ファイル操作権限が強力なため、コンテナ隔離（非 root・`read_only`・`cap_drop: ALL`・`docker.sock` 非マウント・DB ネットワーク分離）と `opencode.json` の明示的 `permission` 設定（夜間バッチ用と対話用のエージェント分離）を実装前に必ず組み込む
- **Discord・スケジューラは外側で補う**：OpenCode 単体には Discord Bot 常駐や Cron 配信機能が内蔵されていないため、定時実行（systemd timer 等）と Discord Webhook 通知は外側の薄いラッパーで繋ぐ
- **さくらの AI Engine 無料枠とプレビューモデルの制約**：
  - 無料枠はチャット補完全体で **月 3,000 リクエスト**（1 日約 100 回目安・超過時はレート制限のみで自動課金なし）。Heartbeat 等の定期ポーリングは回さず、日次バッチ・必要時呼び出しに絞る
  - `preview/` 付きモデル（`preview/DeepSeek-V4.1-Flash-1017` 等）は試験提供のため予告なく終了し得る。設定ファイルでモデル ID を外出しし、`gpt-oss-120b` や予備プロバイダーへ即切替できる構成にする
  - プロンプトキャッシュ非対応・連続呼び出し時の一時的な `429` や一部パラメータ無視があるため、実行タイムアウト・控えめな出力上限・日次回数上限をラッパー側で設ける
