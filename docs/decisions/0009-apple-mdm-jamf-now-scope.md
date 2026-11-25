# Apple MDM は Apple Business なしの Jamf Now 無料枠で最小運用に留める

状態： 運用中（完了：`macOS ノート PC`・`iOS 端末` の 2 台を Jamf Now 無料枠へ登録し、最小セキュリティ設定と FileVault 復旧キー退避まで確認済み）

## 決定

自宅基盤における Apple 端末（`macOS ノート PC`・`iOS 端末`）の MDM 層として、**Apple Business（旧 ABM）を使わない Jamf Now 無料枠（3 台まで無料）** を採用し、以下の最小範囲で運用を区切る。

- **認証・登録方式**：個人の Apple Account で発行した APNs 証明書と **オープン登録** を使用（登録完了後にオープン登録は無効化済み）
- **対象端末とブループリント**：OS 別にブループリントを分離して 2 台を管理
  - **`macOS ノート PC`（Apple Silicon / macOS 11+・自動監視対象）**：パスコード要件、FileVault 有効化および個人復旧キーの Jamf 側エスクロー（既存暗号化済みのため `fdesetup changerecovery -personal` でローテーション実施済み）
  - **`iOS 端末`（iOS・非監視）**：パスコード要件・リモートロック／ワイプ待機
- **既存基盤（`home-infra`）との住み分け**：
  - **Fleet Free（自前・API あり）**：Mac を含む PC 3 台のソフトウェア棚卸し、CVE 脆弱性検知、OpenCode からの定時巡回・Discord 通知
  - **Jamf Now（無料枠・SaaS）**：Apple 2 台のセキュリティ設定強制、Mac の FileVault 復旧キー退避、リモートロック／ワイプ

※ 詳細は [デバイス管理の設計（Fleet・NetBox・Ansible）](../device-management-design.md) を参照。

## 要件

- 個人利用の自宅端末 2 台に対し、月額コストゼロで最低限のセキュリティ強制と紛失時対策（リモートロック／ワイプ・FileVault 復旧キー退避）を備えられること
- 法人・個人事業主前提の組織審査（D-U-N-S 番号や公式事業書類の提出）に依存せずセットアップできること
- 稼働中の Fleet Free（`fleetd` / `osquery`）・NetBox・Zabbix・Authentik と責務が衝突せず、保守負担を増やしすぎないこと

## 理由

- **個人 Apple Account のみで完結**：Apple Business の組織確認（2 段階審査）を挟まず、APNs 証明書の発行だけで即日運用へ載せられた
- **Fleet Free の弱点を補完できる**：Fleet Free（無料版）では FileVault 復旧キーの自動エスクローや iOS 管理が対象外だが、Jamf Now 無料枠を重ねることで「観測・CVE・自動化は Fleet、設定強制・鍵退避・iOS 端末は Jamf Now」と役割が噛み合う
- **Jamf Now の仕様上限を見極めた現実的な区切り**：Jamf Now には公開 REST API・Webhook・カスタムドメイン・Active Directory バインド・Authentik 標準連携がいずれもないため、NetBox / Zabbix / OpenCode との自動連携を追わず Web UI 完結の最小 MDM 枠に留めるのが最も保守性が高い

## 落とした案

- **Apple Business（旧 ABM）での組織確認・ドメイン検証（`example.com`）・ADE 導入**：
  - 2026 年の統合版 Apple Business でも登録から 60 日以内に 2 つの確認方法（ドメイン検証に加えてビジネス ID や公式事業書類等）による組織審査通過が必要。純粋な個人宅 2 台の用途に対して手続きと審査要件が過剰なため見送った
- **`macOS ノート PC` の MDM を Fleet Free 側へ寄せる案**：
  - 1 端末 1 MDM の原則があるため一方へ絞る必要がある。Fleet Free 単体では FileVault 復旧キー自動退避や iOS 端末 の一括管理が不足するため、Apple MDM は Jamf Now へ統一し、Fleet は `osquery` 可視化のみ併用する形に決めた
- **Jamf Now と自宅ドメイン／Authentik／NetBox / Zabbix の直接統合**：
  - Jamf Now は公開 REST API・Webhook・AD バインド・カスタムドメイン非対応であり、内蔵 Jamf Connect（パスワード同期）も Microsoft Entra ID / Okta 限定のため見送った
- **Jamf Pro または自前 OSS MDM（NanoMDM 等）への移行**：
  - Jamf Pro は最低台数・年額契約のコストが個人宅に合わない。NanoMDM や Fleet の MDM 機能へ寄せて API 自動化する案は、2 台規模に対して自前構築・維持コストが勝るため今回は採用せず、将来 API 連携まで必要になった際の再検討候補とする

## 注意

- **APNs 証明書の年次更新（必須）**：APNs 証明書は 12 か月ごとに **作成時と同じ Apple Account** で更新する必要がある（別アカウントで作り直すと全端末の再登録になる）。更新時期（毎年 11 月ごろ）のリマインダーを設定済み
- **非 ADE 登録の制約**：オープン登録のため MDM プロファイルは端末側から手動削除でき、初期化時の自動再登録（ゼロタッチ）や VPP アプリサイレント配布は使えない。また `iOS 端末` は非監視（Unsupervised）のため監視必須の制限項目は効かない
- **復旧キーと秘密情報の扱い**：APNs 証明書・秘密鍵および FileVault 個人復旧キーの値はリポジトリへ記録しない。新しい FileVault 復旧キーは Jamf Now 側への退避確認とあわせて別途安全な保管先にも控える
- **任意拡張の余地**：Mac 向けファイアウォール／Gatekeeper のカスタムプロファイル（`.mobileconfig`）や iOS 向け自宅基盤 Web Clip 配布は追加可能だが、現時点は最小ベースラインで完了とする
