# backlog: WAFL-PEFT — 人間判断待ち事項 / 自動判断の記録

新しいものを常に先頭に追記する（逆時系列）．
- 可逆な暫定判断: `## B{n} [auto-decided YYYY-MM-DD] 題目`（状況・自動選択・根拠・要レビュー）
- 不可逆・危険な事項: `## B{n} [needs-human YYYY-MM-DD] 題目`（Slack で @mention 済みと明記）

---

## B8 [auto-decided 2026-08-05] Iter15: merge JSONL メトリクス化 + W3 対比実験

- 状況: Iter14 で W3（merge_include_self）修正を実施したが、P2P 接続修正も同時に適用された
  ため、accuracy 20.0% の要因が W3 由来か P2P 修正由来か分離不能。W3 レバーは「保留」と判定。
  また merge イベントが print() のみで JSONL メトリクスに記録されていないため、マージの発生を
  定量観測できない。
- 自動選択: Iter15 で以下の 2 点を行う。
  (1) **merge イベントの JSONL メトリクス化**: `src/client.py` の merge ループで、print() 出力
      だけでなく JSONL メトリクスファイルにも merge イベントを書き出す。フィールド:
      timestamp, peer_id, num_peers_merged, merge_includes_self (true/false)。
  (2) **W3 対比実験（control x 2）**: W3 あり control 実験を 1 回、W3 なし control 実験を 1 回
      実行。accuracy の差が W3 の独自効果。P2P 接続修正は既に完了済みなので固定。
      ノード数: 10（既存構成継続）。接触パターン: 既存（rwp_n10_a0500_r100_p10_s42.json）。
- 根拠: W3 修正は WAFL 原典との整合性を取るために必要だが、その効果を測定するには
  merge の発生を定量観測できる環境が必須。また対比実験なしに W3 の採用/棄却を判定すると、
  再度単一レバー原則に違反する。
- 要レビュー: merge JSONL メトリクスのスキーマ設計は実装時に確認。W3 対比実験は 2 回の実行
  が必要（約 22 分 x 2 = 約 44 分）ため、GPU 環境の確保に注意。
- 補足: per-peer accuracy の取得も併せて行うべきだが、post-experiment evaluation の実装
  不備が原因。まずは merge JSONL 化に集中し、per-peer accuracy は Iter16 以降に回す。

## B7 [auto-decided 2026-08-04] Iter14: W3（merge_include_self）着手 + per-peer ログ収集不具合修正

- 状況: Iter13 で同期バリアが accuracy 改善（20.0% vs 7.5%）を確認．ただし control の baseline 未達成（7.5% < 8.5%）．
  F2 で特定された「マージが自ノードを含まない」実装乖離が低 accuracy の主因である可能性が高い．
  また per-peer ログ収集の 50% 欠落（peer 0,2,3,8,9）と `_final.log` 欠落（peers 2,3,8,9）も修正必要．
- 自動選択: Iter14 で以下の 2 点を行う．
  (1) W3（`merge_include_self`）: `src/client.py` の merge ロジックに自ノードを含める修正．
      接触相手 1 台の場合でも「相手の重みへの置換」ではなく「自ノードを含む平均」へ．
  (2) per-peer ログ収集不具合修正: `_final.log` の欠落と per-peer ログ収集失敗の原因を調査・修正．
      `analyze.py` のログ収集ロジックまたは `start_clients.py` の rsync 設定に問題がある可能性．
- 根拠: W3 が control の低 accuracy の主因であれば、同期バリアの有効性評価も再検証が必要．
  per-peer データが欠落すると統計解析が不可能になるため、必須修正．
- 要レビュー: W3 の実装計画は planner が立案．5 ノード構成での再測定も併せて検討する．

## B6 [auto-decided 2026-08-04] Iter13 結果: 同期バリアは accuracy 改善（20.0% vs 7.5%）・W3 着手へ

- 状況: Iter13 で control（非同期 P2P）と treatment（同期バリア `WAFL_P2P_SYNC=1`）を
  10 ノードで実行．treatment の最終 accuracy 20.0% は control の 7.5% を +12.5pt 上回った．
  control は不安定（5%→27.5%→15%→7.5%，peak→final -20pt），treatment は単調増加
  （5%→10%→12.5%→20%）．同期バリアは stale weight による収束不安定化を抑制し，
  収束を促進している可能性が高い．

- 自動選択: 次イテレーション（Iter14）では W3（`merge_include_self`）を着手する．
  理由: control の accuracy 7.5% が baseline 8.5% も満たさない原因を調べるため．
  F2 で特定された「マージが自ノードを含まない」実装乖離が，control の低 accuracy の主因であり，
  同期バリアの有効性評価にも影響する．W3 を修正した上で control を再測定し，
  baseline 再現を確認する．

- 併せて: 5 ノード構成で同期バリアを再測定する案も検討候補（10 ノード化の影響を切り分けるため）．
  planner が W3 と 5 ノード再測定のどちらを優先するか判断する．

- 要レビュー: 同期バリアの accuracy 改善効果は有意だが，per-peer データの 50% 欠落と
  Server Ready 9/10 の不具合がある．これらは次イテレーションで修正する必要がある．

---

## B5 [auto-decided 2026-08-04] Iter13: control 再測定 + treatment 再実験（ログ永続化・peer 状態確認）

- 状況: Iter12 の両条件で accuracy 5.0%（baseline 8.5% 以下）の異常値．treatment で peer 欠落（4/5），
  クライアントログ消失により `WAFL_P2P_SYNC=1` の有効性検証不能．本次目的（同期 vs 非同期 throughput 比較）は不成立．
- 自動選択: Iter13 で以下の 3 点を先に行う．
  (1) control 再測定: 既存最良構成（rank16/alpha32/lr2e-4/dropout0.15/grad_accum8/seq208/window1500s）で
      baseline 再現を確認し，peer 5 台が正常動作するか検証する．
  (2) treatment 再実験: (a) 全 peer の GPU 状態を事前確認，(b) クライアントログをコンテナ削除後も残る場所へ
      永続化，(c) `client.py` 起動時に `p2p_sync_enabled` をログ出力する処理を追加．
  (3) `settings.json` の `experiment_name` を control/treatment で区別する値に切り替える．
- 根拠: Iter12 のデータ不備を解消しないと同期バリアの有効性検証ができない．control の再測定で
  環境全体の問題か Iter12 固有の問題か切り分ける必要がある．
- 要レビュー: `client.py` への `p2p_sync_enabled` ログ出力追加は実装変更を含む．planner が実装計画を立てる．
- 補足: levers の W1（評価解像度）は最優先だが，本次は Iter12 の後処理（再測定）を最優先とする．
  W1 へは Iter14 以降に着手する予定．

## B4 [auto-decided 2026-07-26] Iter12 の実験ブロッカー解消（GPU 競合の解消を確認）

- 状況: B2 で人間が選択した B1 案「5 台構成のまま GPU 競合の解消を待つ」の待機条件が満たされたかを確認した．
  2026-07-26 時点で wafl500〜509 の 10 台すべてが到達可能で，使用中 VRAM は各 2.7〜3.9GB
  （空き約 8.4GB/台）．Iter12 を止めていた外部競合（namit/DETR）は終了している．
- 自動選択: `config/hosts.txt` を元の 5 ノード構成（192.168.15.100 / .102 / .103 / .108 / .109）へ復帰させ，
  Iter12 の実験フェーズを再開可能な状態にした．
- 根拠: 接触パターンファイルは `rwp_n05_a0500_r100_p10_s42.json`（n=5）のみであり，5 ノード構成が
  `_wait_for_ready()` の `expected=5` と一致してデッドロックが解消する．B2 で人間が既に承認した方針の
  実行であり，新規の判断ではない．
- 要レビュー: 実験再開前に他プロジェクト（expert-mesh）と GPU プールを共有している点に注意．
  両者を同時に走らせない運用が必要．
- 補足: Iter12 の指標は wall-clock throughput であり，下記 B3 の統計的制約を受けないため，
  W1 の完了を待たずに実行してよい．

## B3 [auto-decided 2026-07-26] Iter1〜11 の「収束済み」判定を保留し，測定系の立て直しを最優先とする

- 状況: 先行研究の再調査と既存結果の統計的再検討（`plans/p0001_research_direction_2026-07.md`）により，
  評価が 40〜80 問しかなく，p≈0.225 における二項 SE が 4.67〜6.60pt，80% power で検出可能な最小差が
  18.5〜26.2pt であることが判明した．一方の成功条件は「+2pt 以上」であり，**原理的に判定不能**だった．
  +14pt の全体効果（z=2.49）を除き，rank32 の「大幅悪化」(z≈1.0) を含むほぼすべての判定が
  測定ノイズの範囲内である．
- 自動選択: (1) 「約 22% が固定制約下の上限で収束済み」という Iter11 時点の判定を**保留**とする．
  (2) `config.yml` の levers を全面改訂し，W1（評価解像度 500 問以上 + McNemar + Wilson CI）と
  W2（`max_seq_len` 208→512．ベースライン 8.5% の健全性確認）を最優先に置いた．
  (3) success_criteria を，W1 完了までは accuracy を採否根拠にしない暫定基準へ変更した．
- 根拠: Bowyer+ ICML2025 Spotlight (arXiv:2503.01747) は n<数百 で CLT ベースの誤差棒が不確実性を
  大幅に過小評価すると示している．測定系を直さずにレバーを振り続けても，結果は解釈できない．
- 併せて発見した実装上の乖離（同 plans の F2/F3．レバー W3/W5 として登録済み）:
  - `src/client.py:882-902` のマージが自ノードを含まず，`:1151-1155` で `param.copy_()` により
    完全上書きしている．接触相手 1 台の場合は「相手の重みへの置換」であり，WAFL 原典
    (Ochiai+ arXiv:2205.11779) の定式化と異なる．
  - `src/client.py:1523` の `set_deterministic_seed(PEER_ID)` により `lora_A` の初期値がノード毎に異なる．
- 要レビュー: 過去 11 イテレーションの結論を再評価する必要があるため，論文（`docs/paper.tex`）に
  既存の判定を根拠として書いている箇所があれば，W1 完了まで主張を弱める必要がある．

## B2 [resolved-by-human 2026-07-18] Iter12 実験: hosts.txt 2台構成でのデッドロックへの対応
- 状況: 前回セッションで外部 GPU 競合により `config/hosts.txt` を 5 台→2 台へ縮小していたが，
  `src/server.py` の `_wait_for_ready()` は contact pattern ファイル（5 peer 前提）から
  `expected=5` を導出し，タイムアウトなしで無限待機する実装のため，2 台では実験が永久に開始しない
  デッドロックが発生した（詳細は journal.md `### 実験 (Iter12)`）．
- 論点: 5 台構成の GPU 競合解消を待つか（比較可能・時期不確定），2 台用に作り直して継続するか
  （即着手可・単一レバー原則から逸脱し 5 ノード結果と比較不能）．
- Slack で @mention の上ユーザーに確認．**回答: 5 台構成のまま GPU 競合解消を待つ（B1 案を採用）**．
  単一レバー原則・Iter1〜11 との比較可能性を優先．

## B1 [needs-human 2026-07-18] Iter1〜11 収束後の次フロンティアの選定
- 状況: 学習ハイパラの単一レバー探索は「約 22% が固定制約下の上限」で収束済み（journal 参照）．
  以降の有意な前進は paper.tex の展望（逐次方式比較・ノード数スケール・無線環境模擬・不均一計算の影響）に移るが，
  いずれも新規実装またはノード確保を伴い，研究方針の選択を含む．
- 論点: どのフロンティアを次サイクルの主対象にするか．特にノード数スケールは物理ノード確保（数十台）が要る．
- 暫定方針（planner が着手前に確認）: 新規実装のみで着手できる「逐次方式との throughput 比較
  （client.py に P2P 同期実行フラグを追加）」を可逆な第一候補とし，ノード確保が要るスケール検証は人間判断待ちとする．
- Slack: 初回サイクル開始時に @mention してこの選定を仰ぐ．
