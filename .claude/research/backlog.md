# backlog: WAFL-PEFT -- 人間判断待ち事項 / 自動判断の記録

新しいものを常に先頭に追記する（逆時系列）．
- 可逆な暫定判断: `## B{n} [auto-decided YYYY-MM-DD] 題目`（状況・自動選択・根拠・要レビュー）
- 不可逆・危険な事項: `## B{n} [needs-human YYYY-MM-DD] 題目`（Slack で @mention 済みと明記）
- 人間が回答して決着した事項: `## B{n} [resolved-by-human YYYY-MM-DD] 題目`
- 決着した項目には末尾に `- 決着（YYYY-MM-DD）:` 行を追記し，未決の項目と区別できるようにする．

**未決（人間判断待ち）**: なし．B10 で次イテレーションの方針が人間により確定した．

---

## B10 [resolved-by-human 2026-08-05] Iter17 の方針: 測定系の立て直し（W1+W2）と 5 ノード + 評価専用 5 ノード構成への移行

人間へ 4 点を提示し，以下の回答を得た．**Iter17 はこの決定に従って設計する**．

### 決定 1: Iter16 は考察フェーズを回して正式に閉じる

`state.json` は `phase=reflect / status=running`．rc-reflector が採否判定の確定・journal の
`### Iteration 16 実行済み` 記入・Slack/Notion 報告・commit まで行う．W3 は採用（B9 の決着参照）．

### 決定 2: 次のレバーは W1 + W2 を同時に振る

- W1: 評価問題数を 40 → 500 以上へ（`settings.json` の `global_eval.sample_limit`，および
  post-experiment evaluation の `_POST_EVAL_SAMPLE_LIMIT`）．併せて McNemar 対比較と
  Wilson 95% CI を `src/compare_runs.py` に導入する．
- W2: `training.max_seq_len` を 208 → 512 へ（GSM8K 学習例の 32.5% で回答末尾が欠落している問題の解消）．
- **単一レバー原則を意図的に破る**．根拠は `config.yml` の W2 note が「このレバーは accuracy の絶対水準を
  大きく動かす可能性があるため，W1（評価解像度）と同時に実施し，以降の全比較のベースラインを
  取り直すこと」と明示的に指示しているため．**Iter17 は「ベースラインを取り直すイテレーション」として
  位置付け，個別レバーの効果測定は行わない**．
- OOM リスク: 系列長 2.46 倍で活性化メモリが増える．OOM した場合は 320 を中間案とし，
  それでも詰まるなら W2b（PLE の CPU オフロード）を先に実施する．

### 決定 3: 学習 5 台 + 評価専用 5 台に分割する

`config/hosts.eval.txt` の 5 台（`.101/.104/.105/.106/.107`）が `config/hosts.txt`（10 台）と
完全に重複しており，評価専用ホストが実質ゼロだった．学習ノードを 5 台へ減らし，残り 5 台を
評価専用ホスト（`eval_worker.py`）として使う．学習ノードの VRAM を評価で圧迫せず，
学習中から随時評価できる．

- 学習ノード: `.100 / .102 / .103 / .108 / .109`（`hosts.eval.txt` の補集合．B4 の 5 ノード構成と同一）
- 評価ホスト: `hosts.eval.txt` の現行 5 行（行順＝担当 peer_id）
- 学習クライアントは `WAFL_SELF_EVAL=0` で自己評価を無効化する

### 決定 4: watchdog を再起動して自律継続する

`bash ~/.claude/skills/research-cycle/scripts/start_watchdog.sh wafl-peft` で起動する．
不可逆・危険な判断が出たら従来どおり backlog 経由で Slack に @mention する．

---

### Iter17 実装の前提条件（**この順序で行わないと実験が起動しない**）

1. **`config/hosts.txt` を 5 台へ縮小する**（`.100 / .102 / .103 / .108 / .109`）．
2. **n=5 の接触パターンを生成する**: `mise run setup:contact-pattern -- --n-time 1500`．
   `generate_contact_pattern.py` は `hosts.txt` の行数からノード数を決めるため，**必ず手順 1 の後**に実行する．
   生成物は `data/contact_pattern/rwp_n05_a0500_r100_p10_s42.json`．
   **現在 `data/contact_pattern/` には n=10 のパターンしか無い**．`src/server.py` の `_wait_for_ready()` は
   接触パターンから期待 peer 数を導出しタイムアウト無しで待つため，5 台構成に n=10 パターンを
   組み合わせると**実験が永久に開始しないデッドロックになる**（Iter12 で実際に発生．B2 参照）．
3. **`settings.json` の `experiment.contact_pattern_file` を n=5 のファイル名へ変更する．**
4. **`mise run setup:data` でシャードを再生成する**．5 ノードへ戻すとシャードは
   672 → 約 1345 件/peer に戻る（下記「判明した事実」2 を参照）．
5. **評価ホストへ配布・起動する**: `mise run deploy:eval` の後に評価ワーカーを起動する
   （`deploy:eval` は評価ホストへ GSM8K データセットキャッシュも配る）．

### 併せて修正する実装不具合（人間判断は不要．実装フェーズで対処する）

**per-peer accuracy が Iter14 以降ずっと全 peer 0.0% だった原因を特定した**:
`src/deploy_distribute.py:203-211` は `cache/datasets/gsm8k` を `_EVAL_MODE` のときだけ配布するが，
既定は `WAFL_SELF_EVAL=1`（学習ノードが自己評価）である．学習ノードに GSM8K の parquet が無いため
`gsm8k_eval.load_gsm8k_val_data()` が空リストを返し，self-eval が黙ってスキップされていた
（サーバーの global eval はサーバー自身にキャッシュがあるため動いていた）．
決定 3 で評価を専用ホストへ委譲するなら `deploy:eval` が配るので解消するが，
**学習ノードの自己評価へフォールバックする経路（`WAFL_SELF_EVAL=1`）を残すなら，
学習ノード向け deploy にも datasets の配布を追加する必要がある**．

### 判明した事実（次の planner が前提にすべきもの）

1. **比較可能な実験は Iter14 以降の 4 本のみ**（Iter14ctrl×2 / Iter15 ctrl+treat / Iter16 ctrl+treat）．
   Iter1〜13 は P2P 重み交換が成立しておらず孤立学習と同等．
2. **accuracy の絶対水準の低下（過去 21.5〜22.5% → Iter16 の 15.0〜17.5%）は評価系ではなく，
   10 ノード化によるシャード半減が最有力**．実測で現行の学習シャードは 672 件/peer
   （5 ノード時代は約 1345 件/peer．`validation_split: 0.1` を除いた 6725 件を 10 分割）．
   `config.yml` の W8 note が「総データ量固定なら 1 ノード 1345→747 に半減し，過学習という既存の
   診断を悪化させる方向」と事前に警告していたとおりの現象である．
   決定 3 で 5 ノードへ戻すため，この交絡は Iter17 で解消する．
3. **Iter17 では 3 つの条件が同時に変わる**（評価解像度・`max_seq_len`・ノード数 10→5）．
   したがって **Iter14〜16 との accuracy 比較は成立しない**．Iter17 の結果は「新しいベースライン」
   として扱い，以降のレバーはこれを起点に 1 つずつ振る．

## B9 [auto-decided 2026-08-05] Iter16: merge_includes_self動的値化 + W3再評価

- 状況: Iter15でmerge JSONLメトリクス化は成功（248/246件記録）．W3対比実験でloss改善（-4.0%）
  を観測したが，accuracy比較はTreatmentのglobal_eval.log未生成で未取得．
  `merge_includes_self`が`src/client.py:1027`で`False`にハードコードされており，
  W3適用有無をメトリクスから判定できない計測バグがある．
- 自動選択: Iter16で以下の2点を行う．
  (1) `merge_includes_self`の動的値化: W3適用時は`true`，未適用時は`false`を出力．
      これにより次実験でW3適用有無をメトリクスから判定可能になる．
  (2) W3再評価実験: `merge_includes_self`を動的値にした上で，W3あり/なしの対比実験を
      再実行．global_eval.logの保存を確認した上でaccuracy比較を行う．
- 根拠: merge JSONLメトリクス化は完了したが，W3のaccuracyへの独自効果は判定不能．
  `merge_includes_self`のハードコードを修正しないと，次実験でもW3適用有無が不明．
  loss改善傾向（-4.0%）は有意なシグナルなので，accuracyでも確認する必要がある．
- 要レビュー: `merge_includes_self`の動的値化は計測系の変更．W3修正（self重み追加）の有無を
  コードで制御する仕組みが必要．plannerが実装計画を立てること．
- 補足: self-evalスキップ（GSM8K validation data not available）はIter16でも解消しない可能性
  が高い．per-peer accuracyは次イテレーション以降に回す．
- 決着（2026-08-05）: Iter16 として実施した．(1) 動的値化は成功（control 全 265 件 `false` /
  treatment 全 242 件 `true`）．(2) W3 は **採用**（最終 loss 0.517 → 0.364，per-peer の最終 loss
  標準偏差 0.406 → 0.171）．既定 `WAFL_MERGE_INCLUDE_SELF=1` として永続適用する．
  accuracy は両条件とも peak 17.5% でノイズ範囲内であり，判定は W1 完了後に再実施する．
  補足の予想どおり self-eval スキップは未解消で per-peer accuracy は取得できていない．
  ※ Iter16 の考察フェーズ（journal の `### Iteration 16 実行済み`）は未記入である．

## B8 [auto-decided 2026-08-05] Iter15: merge JSONL メトリクス化 + W3 対比実験

- 状況: Iter14 で W3（merge_include_self）修正を実施したが，P2P 接続修正も同時に適用された
  ため，accuracy 20.0% の要因が W3 由来か P2P 修正由来か分離不能．W3 レバーは「保留」と判定．
  また merge イベントが print() のみで JSONL メトリクスに記録されていないため，マージの発生を
  定量観測できない．
- 自動選択: Iter15 で以下の 2 点を行う．
  (1) **merge イベントの JSONL メトリクス化**: `src/client.py` の merge ループで，print() 出力
      だけでなく JSONL メトリクスファイルにも merge イベントを書き出す．フィールド:
      timestamp, peer_id, num_peers_merged, merge_includes_self (true/false)．
  (2) **W3 対比実験（control x 2）**: W3 あり control 実験を 1 回，W3 なし control 実験を 1 回
      実行．accuracy の差が W3 の独自効果．P2P 接続修正は既に完了済みなので固定．
      ノード数: 10（既存構成継続）．接触パターン: 既存（rwp_n10_a0500_r100_p10_s42.json）．
- 根拠: W3 修正は WAFL 原典との整合性を取るために必要だが，その効果を測定するには
  merge の発生を定量観測できる環境が必須．また対比実験なしに W3 の採用/棄却を判定すると，
  再度単一レバー原則に違反する．
- 要レビュー: merge JSONL メトリクスのスキーマ設計は実装時に確認．W3 対比実験は 2 回の実行
  が必要（約 22 分 x 2 = 約 44 分）ため，GPU 環境の確保に注意．
- 補足: per-peer accuracy の取得も併せて行うべきだが，post-experiment evaluation の実装
  不備が原因．まずは merge JSONL 化に集中し，per-peer accuracy は Iter16 以降に回す．

## B7 [auto-decided 2026-08-04] Iter14: W3（merge_include_self）着手 + per-peer ログ収集不具合修正

- 状況: Iter13 で control の accuracy が 7.5% と baseline 8.5% を下回った主要因は，`src/client.py` の merge ループが自ノードの重みを平均に含めていないこと（W3/F2）である．
  接触相手 1 台の場合（RWP ペア接触では通常こうなる），`count=1` であり `merged` は相手の重みそのものになる．自ノードの重みは平均ではなく置換され，直前のマージ以降の自ノード学習が全て破棄される．
  また per-peer ログ収集の 50% 欠落（peer 0,2,3,8,9）と `_final.log` 欠落（peers 2,3,8,9）も修正必要．
- 自動選択: Iter14 で以下の 2 点を行う．
  (1) W3（`merge_include_self`）: `src/client.py:882-902` の merge ループに自ノード重みを含める．
      接触相手 1 台の場合でも「相手の重みへの置換」ではなく「(self + remote) / 2」へ．
  (2) per-peer ログ収集不具合修正: `_final.log` の欠落と per-peer ログ収集失敗の原因を調査・修正．
      `analyze.py` のログ収集ロジックまたは `start_clients.py` の rsync 設定に問題がある可能性．
- 根拠: W3 が control の低 accuracy の主因であれば，同期バリアの有効性評価も再検証が必要．
  per-peer データが欠落すると統計解析が不可能になるため，必須修正．
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
  評価が 40〜80 問しかなく，p=0.225 における二項 SE が 4.67〜6.60pt，80% power で検出可能な最小差が
  18.5〜26.2pt であることが判明した．一方の成功条件は「+2pt 以上」であり，**原理的に判定不能**だった．
  +14pt の全体効果（z=2.49）を除き，rank32 の「大幅悪化」(z=1.0) を含むほぼすべての判定が
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
- 要レビュー: 過去 11 イテレーションの結論を再評価する必要があるため，外部へ説明する際に
  既存の判定を根拠として使っている箇所があれば，W1 完了まで主張を弱める必要がある．
- 進捗（2026-08-05）: W3（F2 のマージ乖離）は Iter16 で **採用済み**（B9 参照）．W5（`lora_A_only`）の
  前提となる F3（`lora_A` の初期値がノード毎に異なる）は未修正．**W1・W2 はいずれも未着手**であり，
  「W1 完了まで accuracy を採否根拠にしない」という制約は現在も有効である．

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
  以降の有意な前進は今後の研究フロンティア（逐次方式比較・ノード数スケール・無線環境模擬・不均一計算の影響）に移るが，
  いずれも新規実装またはノード確保を伴い，研究方針の選択を含む．
- 論点: どのフロンティアを次サイクルの主対象にするか．特にノード数スケールは物理ノード確保（数十台）が要る．
- 暫定方針（planner が着手前に確認）: 新規実装のみで着手できる「逐次方式との throughput 比較
  （client.py に P2P 同期実行フラグを追加）」を可逆な第一候補とし，ノード確保が要るスケール検証は人間判断待ちとする．
- Slack: 初回サイクル開始時に @mention してこの選定を仰ぐ．
- 決着（2026-08-05 追記）: B2 で人間が「5 台構成のまま GPU 競合の解消を待つ」と回答し，暫定方針の
  「逐次方式との throughput 比較」を Iter12〜13 で実施した（`WAFL_P2P_SYNC`）．
  その後 B3 で研究方針そのものを改訂したため，本項目は解決済みとして扱う．
  ノード数スケール（数十台規模）は未着手のまま `config.yml` の `research_frontier` に残っている．
