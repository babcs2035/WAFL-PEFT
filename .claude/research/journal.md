# 実験ジャーナル: WAFL-PEFT

research-cycle が読み書きする実験ジャーナル．**新しいイテレーションを常に先頭へ挿入する（逆時系列）**．
1 イテレーション = 単一レバー変更．各ブロックに仮説・単一レバー・成功条件（planner 記入）と，
変更・結果・判定・学び（reflector 記入）をまとめる．
Iter1〜11 の詳細な原本は `~/.claude/plans/luminous-purring-hickey.md` にある（本ファイルは要点の凝縮）．

---

## 研究方針の再検討（2026-07-26）— Iter1〜11 の「収束済み」判定を保留

先行研究の再調査（tavily）とリポジトリの実測により，既存の結論の前提が崩れたため方針を改訂した．
**詳細な提案は `plans/p0001_research_direction_2026-07.md`，出典付きの全調査記録は
`docs/d0001_literature_survey_2026-07.md` にある．** 以下は要点のみ．

### 実測で確定した事実

1. **評価解像度の不足（F1）**: 評価 40〜80 問では p≈0.225 の二項 SE が 4.67〜6.60pt，80% power の
   最小検出可能差は 18.5〜26.2pt．成功条件「+2pt 以上」は原理的に判定不能だった．
   +14pt の全体効果（z=2.49）を除き，rank32 の「大幅悪化」(z≈1.0) を含むほぼ全判定がノイズの範囲内．
2. **`max_seq_len: 208` が学習データの 32.5% を壊している（F5・実測）**: GSM8K train 7,473 件を
   実トークナイザーで測定．full_text は平均 188.1 / 中央値 175 / p90 282 / 最大 543．
   208 では **2,428 件 (32.5%) で回答末尾が欠落**し，`src/client.py:414` は完全欠落（1 件）しか
   除外しないためそのまま学習される．評価は `#### N` を抽出して採点するので学習と評価が食い違う．
   512 で切り詰めはほぼ解消（残り 3 件）．
3. **VRAM 制約の本体は PLE の非量子化（F6・実測）**: `model.safetensors` のヘッダ実測で
   `embed_tokens_per_layer` [262144,8960] BF16 = 4.375 GiB，embed 系合計 **5.162 GiB / 全体 9.543 GiB
   （54%）**．bitsandbytes は `nn.Linear` のみ量子化するためこの 54% は fp16 常駐．
   重み合計は約 6.4 GiB になる．因果は
   「PLE 非量子化 → VRAM 逼迫 → seq 208 へ削減 → 回答形式の欠落 → 低 accuracy」である．
4. **マージが WAFL 原典と異なる（F2）**: `src/client.py:882-902` は remote peer のみを平均し
   （自ノードが分母に入らない），`:1151-1155` で `param.copy_()` により完全上書きする．
   接触相手 1 台なら「相手の重みへの置換」で自ノードの学習が破棄される．
5. **LoRA A の初期値がノード毎に異なる（F3）**: `:1523` の `set_deterministic_seed(PEER_ID)` 由来．
6. **PT/IT の食い違い（F7）**: `chat_template` が無く PT 版だが README は `-it` を 4 箇所で参照．

### 文献調査の要点

- **WAFL 原典 (arXiv:2205.11779) は n=10 固定**であり，RWP 設定も現行の a0500_r100_p10 と一致する．
  10 ノード化は「原典と同じ土俵に戻す」正当化になる．ただし総データ量固定ならシャードは 747 に半減．
  文献で有意な n 効果が出るのは 10→100→1000 の桁スケールで，5→10 の効果は検出困難と見込まれる．
- 原典は「近傍が 0 のとき局所学習をスキップせよ（過学習を招くため）」と明示している．
- 分散 LoRA 集約は FedSA-LoRA（A のみ共有，GSM8K +0.40pt）・ADF-LoRA（10client・ペア接触 0.1 で
  0.8458→0.8505）が近いが，改善幅は小さい．RoLoRA の +15pt は DFL では再現せず劣化する．
  FedEx-LoRA は 4-bit と原理的に相性が悪い．
- ベースライン 8.5% は Gemma 系の報告水準（Gemma-2 2B PT で GSM8K 5-shot 23.9%）から見て低すぎる．

### 改訂内容

- `config.yml` の levers を全面改訂．W1（評価 500〜1319 問）と W2（seq_len 512）を最優先に置き，
  W2b（PLE の CPU オフロード）・W3（マージに自ノードを含める）・W4（孤立時の学習スキップ）・
  W5/W6（LoRA 集約手法）・W7（MetaMathQA）・W8（10 ノード）・W9（モデル変更）を登録した．
- `success_criteria` を，W1 完了までは accuracy を採否根拠にしない暫定基準へ変更した
  （throughput 等サンプリング誤差の影響を受けない指標は従来どおり判定してよい）．
- Iter12（同期バリアの throughput 比較）は指標が wall-clock であり統計的制約を受けないため，
  GPU 競合の解消（2026-07-26 確認）を受けて `config/hosts.txt` を 5 ノードへ復帰させ再開可能にした．

## 現在の状態（Iter1〜11 の結論・2026-07-12 時点）

**学習ハイパラの単一レバー探索は収束した．** 最良構成は
`rank16 / alpha32 / lr2e-4 / lr_min_ratio0.5 / dropout0.15 / grad_accum8 / seq208`（chunked CE + paged 8bit AdamW）で，
ノード別最終 accuracy ~22.5%・改善 +14pt（全 5 ノード向上），マージピーク ~25%．
約 22% は「小モデル gemma-4-E2B + 難タスク GSM8K + 小シャード（~1345）」に由来する過学習限界であり，
VRAM・接触パターン・モデルを固定した条件下では単一レバーで超えられない．

**次に進むべき方向（paper.tex 展望，config の research_frontier）**: 逐次方式との throughput 比較，
実機ノード数のスケール，無線環境模擬下の通信頑健化，不均一計算進捗の知識収束への影響．
いずれも新規実装またはノード確保を伴うため，planner はイテレーション開始時に backlog へ登録し，
可逆な範囲で着手，スコープ拡大（ノード確保・大規模改修）は人間へエスカレーションする．

---

## Iteration 12（investigate 中）: 逐次（同期）方式との throughput 直接比較

### 調査 (Iter12)

**問い**
1. 現行 P2P 実装は本当に「非同期・都度送信」方式か（同期方式との差分はどこに入れるべきか）．
2. decentralized/federated SGD で同期 vs 非同期の throughput・収束のトレードオフは先行研究でどう整理されているか．
3. 本実験で何を測り，何を落とし穴として避けるべきか（throughput 指標の定義・比較の公平性）．

**分かったこと（現行コードの確認）**
- 現行は完全な**非同期・機会的（opportunistic）交換**である．根拠となる `src/client.py` の箇所:
  - Thread 2（P2P, `p2p_exchange_thread` 644-930 行）は訓練ループと独立に回り，受信は peer ごとに
    **最新 1 件のみ上書き保持**（`receive_buffers` 668-674, 741-743 行），送信は `shadow_version` が
    進んだ時だけ（847 行）で，バリアや相手の応答待ちを一切しない．
  - Thread 3（訓練, 1102-1106 行）はマージ結果を `merge_queue.get_nowait()` で**非ブロッキング取得**し，
    到着していなければ待たずに学習を継続する．マージ反映は累積境界（optimizer.step 後）でのみ行う（1077-1123 行）．
  - よって「訓練ループが通信で止まらない（stall-free）」ことが設計目標で，`analyze.py` は既に
    elapsed と `tokens_per_sec` の相関 ≈0 でそれを検証している（fig01_throughput_flatness）．
  - **同期方式の入れ所**: 訓練ループ内のマージ点にバリアを挿入し，現在接触中の peer との重み交換完了まで
    `merge_queue.get_nowait()` を**ブロッキング取得**へ切り替える（`client.py` の 1102 行付近 + Thread 2 の
    交換完了通知）．env フラグ（例 `WAFL_P2P_SYNC=1`）で分岐するのが可逆で最小侵襲．
- **先行研究の整理**
  - Lian et al., AD-PSGD (ICML 2018, proceedings.mlr.press/v80/lian18a): 非同期分散 SGD は同期 D-PSGD/AllReduce と
    **エポック単位（＝更新回数単位）ではほぼ同等**に収束しつつ，ネットワーク共有・heterogeneous 環境では
    **1 エポックあたり実時間 4〜8 倍高速**．最適 O(1/√K) 収束・worker 数に線形スピードアップ．
  - Dutta et al., "Slow and Stale Gradients Can Win the Race" (AISTATS 2018): 本比較の核心枠組み．
    **error-vs-iterations（更新回数軸）と error-vs-wallclock（実時間軸）を分離**して評価すべき．非同期は
    更新回数あたりの収束レートは同期より低い（staleness ペナルティ）が，1 反復が桁違いに速いため
    **実時間では勝ちうる**．straggler 遅延を確率変数として runtime を解析している．
  - Staleness の一般則（IJCAI 2016 Staleness-Aware ASGD, ICLR 2019 Dai et al., ACL 2019）: 非同期では勾配が
    平均 N-1 更新ぶん古くなる．**反復回数を固定すると非同期は同期より悪化**するが，実時間・straggler 耐性で逆転．
    勾配蓄積（accumulation）が staleness を緩和し最終品質を僅かに上げる例もある．
  - D-PSGD の topology/spectral-gap トレードオフ (Sato et al. 2020, emergentmind D-PSGD): 疎なグラフは
    1 ラウンドが速いが mixing（収束）が遅い．**時変トポロジー（WAFL の接触パターン）では同期バリアが
    短い接触ウィンドウを取り逃す**リスクがある（config の Koloskova 理論検証とも接続）．

**次フェーズ（検討・計画）への示唆**
- **主指標は「time-to-accuracy（実時間軸）」にする．** 更新回数（マージ回数）あたりの accuracy だけで比べると
  同期方式が有利に見え，非同期の throughput 利得を過小評価する（Dutta の落とし穴）．
  accuracy vs wall-clock と accuracy vs マージ回数（or optimizer step）の**両軸で報告**する．
- 測るべき量（多くは既存ログで取得可能）:
  1. throughput = `tokens_per_sec`・steps/s（`analyze.py` 既存），
  2. 同期方式の**バリア待ち時間**（アイドル率）= 既存の compute_duration と stall_duration の分離を流用，
  3. 実験ウィンドウ（時間ベース 1500s）固定での**総 optimizer step 数**（同期はバリア待ちで減るはず），
  4. （任意）非同期の**staleness**＝送信版と反映版の版番号差．
- **公平性の要点**: 本実験は時間ベース制御（1500s 窓）なので，同一実時間での最終 accuracy を主比較にすると
  「非同期の高スループット」仮説の検証として整合する．ただし heterogeneous 前提（5 ノード + 外部 GPU 競合による
  速度ばらつき，MEMORY 参照）が非同期優位の前提なので，**同期実装も非同期と同等に最適化**しないと不公平になる
  （現行非同期は shadow_version 等でチューニング済み）．
- **時変トポロジー固有のリスク**（要注意・計画で明記）: 接触ウィンドウが短いため，同期バリアが交換完了前に
  接触が切れると**知識伝播が起きない**恐れがある．同期方式のバリアには接触打ち切りタイムアウトが要る．
- 可逆な暫定判断（B1）: 新規実装のみで着手できる本比較を第一候補として妥当．ノード数スケールは人間判断待ちのまま．

### 検討・計画 (Iter12)

**仮説**
heterogeneous 環境（5 ノード + 外部 GPU 競合による速度ばらつき）・時変トポロジー下では，同期バリア方式は
straggler 待ちでアイドルが発生し，同一実時間窓（1500s）内の総 optimizer step 数を非同期より減らす．
その結果，非同期（現行）は同一実時間での最終 accuracy を同等以上に保つ（time-to-accuracy で優位）．
一方，更新回数軸では同期が staleness ペナルティ分だけ 1 更新あたりは有利になりうる（Dutta の二軸トレードオフ）．

**単一レバー（今回動かす唯一の軸）**
P2P 交換方式: 「非同期・機会的（現行）」→「同期バリア（`WAFL_P2P_SYNC=1`）」．
学習ハイパラは Iter1〜11 最良の固定点（rank16 / alpha32 / lr2e-4 / lr_min_ratio0.5 / dropout0.15 /
grad_accum8 / seq208）を両条件で共通に据え置く．接触パターン・窓 1500s・評価も共通．

**変更内容の設計（最小侵襲・可逆，`src/client.py`）**
- バリア挿入点は Thread 3 訓練ループのマージ取得（現状 1102〜1106 行の `merge_queue.get_nowait()`）．
  env `WAFL_P2P_SYNC=1` のときだけ **ブロッキング取得 `get(timeout=barrier_timeout)`** へ分岐し，
  現在接触中の peer との重み交換・マージ完了まで累積境界で待機する．既定（未設定/`0`）は現行の
  非ブロッキング経路を一切変えない（後方互換）．
- **バリアのゲート条件**: `state.peer_whitelist`（`whitelist_lock` 下で参照）が空でない時のみブロックする．
  接触相手が居ない孤立区間で待つのは無意味なので，その場合は `get_nowait` にフォールバックし単独前進する．
- **タイムアウト初期値 = 15.0s**（env `WAFL_P2P_SYNC_TIMEOUT_SEC` で可変，`config/settings.json` の
  `communication` セクションに `p2p_sync_timeout_sec` を追加して既定値を持たせる）．
  根拠: 1 optim step は約 5s（0.6〜0.7s/step × grad_accum8），LoRA 重み（float16 で 20〜48MB）の
  1 回の LAN 交換は通常 1s 未満で完了するため 15s は straggler にも十分な余裕がある一方，
  接触ウィンドウに対しては短く，**接触が交換完了前に切れた場合はタイムアウトで抜けて非同期同様に前進**する
  （時変トポロジー下でのデッドロック・知識伝播停止を回避．調査で明示したリスク対策）．
- **計測用フィールド追加**: metric dict（1172 行付近）に `barrier_wait`（このステップでブロッキング取得に
  費やした秒．非同期・非接触時は 0）を追加し，`stall_duration` とは別にアイドルを分離集計できるようにする．
  `analyze.py` 側の集計・作図追加は任意（idle 率 = Σbarrier_wait / Σstep_duration）．

**比較実験の設計**
- 同一セッション内で **control（非同期）と treatment（同期）を各 1 回，連続実行**する．外部 GPU 競合が
  time-varying なため，環境を揃えた同セッション比較を主とする（両者の差は `WAFL_P2P_SYNC` のみ）．
  - control: 既定env（`WAFL_P2P_SYNC` 未設定 = 非同期），最良固定点．
  - treatment: `WAFL_P2P_SYNC=1`，同一固定点．
- セッション予算が逼迫する場合のフォールバック: control を既存の非同期最良 Iter10（同一固定点，
  ノード別 22.5% / マージ 20.0%，Iter8〜10 の band 21.5〜22.5%）で代替し，treatment のみ新規実行する．
- **測定指標（二軸で報告）**
  1. 実時間軸（主）: 固定 1500s 窓での最終ノード別 accuracy・マージ accuracy（= time-to-accuracy）．
  2. throughput: `tokens_per_sec`・steps/s（`analyze.py` fig01 既存）．
  3. 窓内の総 optimizer step 数（同期はバリア待ちで減る想定）．
  4. 同期のバリア由来アイドル率 = Σ`barrier_wait` / Σ`step_duration`（非同期 ≈ 0）．
  5. 更新回数軸（副）: accuracy vs optimizer step / マージ回数（同期は 1 更新あたり staleness 小で有利になりうる）．

**成功条件（measurable）**
同一 1500s 窓・同一セッションで非同期(control)と同期(treatment)を比較し，
(a) 非同期の総 optimizer step 数が同期比 **+15% 以上**多い，かつ (b) 同期のバリア由来アイドル率が **>10%**，
の両方を満たせば「同期バリアが throughput を計測可能に損なう」を確認＝本イテレーションの一次目的達成とする．
最終ノード別 accuracy は，非同期 ≥ 同期を期待し，ノイズ ±5pt 以内で同等なら「throughput 優位・精度同等」，
非同期が **+2pt 超**で上回れば「非同期優位を実証」と判定する．逆に同期が accuracy **+2pt 超**で上回れば
「staleness コスト > throughput 利得」の逆結論として記録する（いずれも Dutta の二軸整理に沿う知見となる）．

### 実装 (Iter12)

**変更ファイル: `src/client.py`**
- 1028〜1029 行の直後（学習ループ突入前）に，同期バリアの設定読み込みを追加．
  - `p2p_sync_enabled = os.environ.get("WAFL_P2P_SYNC", "0") == "1"`．
  - `barrier_timeout = _get_float("communication", "p2p_sync_timeout_sec", 15.0)`，
    `WAFL_P2P_SYNC_TIMEOUT_SEC` が環境変数にあれば `float()` 変換して上書き（変換失敗時はログ出力して
    設定値へフォールバック，例外は握りつぶさない）．
- 旧 1102〜1106 行（現 1121〜1149 行付近）の `merge_queue.get_nowait()` を分岐．
  - `do_optim_step` ブロック冒頭で `barrier_wait = 0.0` を初期化（非同期・非接触時は常に 0 のまま）．
  - `whitelist_lock` 下で `peer_whitelist` の非空判定を行い，`use_barrier = p2p_sync_enabled and has_active_peer`．
  - `use_barrier` が真の場合のみ `merge_queue.get(timeout=barrier_timeout)` でブロッキング取得し，
    経過時間を `barrier_wait` に記録．`queue.Empty` タイムアウト時はログ出力の上，マージなしで前進
    （デッドロック・接触切れ対策，既存の非同期パスと同じ「マージなしで継続」挙動に合流）．
  - `use_barrier` が偽（既定 `WAFL_P2P_SYNC` 未設定，または接触相手なし）の場合は，元の
    `get_nowait()` + `except queue.Empty` をそのまま温存し，コードパス・挙動を一切変更していない．
- metric dict（旧 1172 行付近，現 1215〜1227 行）に `"barrier_wait": barrier_wait` を `stall_duration` の直後へ追加．

**変更ファイル: `config/settings.json`**
- `communication` セクションに `"p2p_sync_timeout_sec": 15.0` を追加（既定値，env で上書き可能）．
- 併せて確認: 編集前から作業ディレクトリ上で `training.learning_rate`（`2e-4`→`0.0002` 表記差），
  `experiment.experiment_name`（`SelfTrain`→`SelfTrainr5`），末尾改行の3点が git HEAD と差分があった
  （本タスク着手前からの未コミット変更で，今回のレバーとは無関係）．CLAUDE.md 規約に従い，この差分には
  一切触れていない（追加した `p2p_sync_timeout_sec` 以外は変更なし）．

**非破壊性の確認**
- `python3 -m py_compile src/client.py` で構文エラーなし．`config/settings.json` は `json.load` で妥当性確認済み．
- コードレビュー: `WAFL_P2P_SYNC` 未設定時は `p2p_sync_enabled=False` → `use_barrier` は常に `False` →
  分岐は必ず既存の `get_nowait()` + `except queue.Empty` 経路のみを通り，`barrier_wait` は常に `0.0`．
  既存の `stall_duration` 計算・チェックポイント・ログ出力ロジックへの変更なし．後方互換を確認．
- 本リポジトリに ruff/mypy/pytest 等の自動検証設定は無く（`pyproject.toml` に lint/test 設定なし，
  `tests/` ディレクトリなし），実機 5 ノードでの動作確認は次の実験フェーズで行う前提のため，
  今回は構文チェックとコードレビューに留めた．

### 実験 (Iter12) — 未実施・構造的ブロッカーで中断

**初期状態確認（16:58 頃の前回試行の引き継ぎ）**
- 前回 16:58:14 頃に開始された起動が，本セッション開始時点（17:00 頃）で実機に残存していた．
  - 管理サーバー（`wafl-ctrl5`）: `wafl-peft-server` コンテナが 16:58 作成で稼働中（`registry` も別途稼働，触っていない）．
  - `192.168.15.108`（peer 0）・`192.168.15.109`（peer 1）: それぞれ `wafl-peft-client-0/1` が 16:58:16 作成で稼働中．
    peer 0 はモデルロード中（GPU 使用量 115MiB のみ），peer 1 はロード完了（~10GB 使用）．
  - `config/hosts.txt` は前回セッション中にユーザー指示で `192.168.15.100/.102/.103` の 3 台をコメントアウト済み
    （外部競合プロセスによる VRAM 逼迫で 4bit 量子化モデルの device_map dispatch が失敗するため）．学習対象は
    `.108`・`.109` の 2 台のみに縮小されていた．
  - `.100/.102/.103` の GPU 状態を本セッションでも再確認: `nvidia-smi` 上でいずれも `python3`（他ユーザーのジョブ，
    PID 1544387/1466326/1564838）が 6.9〜10.7GB を占有し続けており（空き 1.2〜5.0GB），前回セッションの診断
    （外部 GPU 競合）は本セッション時点でも解消していないことを確認した．

**発見した構造的ブロッカー**
- 管理サーバーのログを確認したところ，`[SERVER][Monitor] Ready: 2/5, Registered: 2` が 1 秒間隔で延々と
  出力され続けており，実験が一切開始していなかった．
- `src/server.py` の `_wait_for_ready()`（442〜489 行）を確認: 期待クライアント数 `expected` は
  `_collect_all_peers()`（271〜276 行）が `data/contact_pattern/rwp_n05_a0500_r100_p10_s42.json` の全イベントから
  収集する一意 peer_id 集合であり，このファイルは `{0,1,2,3,4}` の 5 peer 前提で生成されている
  （`python3` で全イベントを走査し確認済み）．`ready_count >= expected` になるまで**タイムアウトなしで
  無限に `time.sleep(1.0)` を繰り返す**実装であり，`hosts.txt` を 2 台に縮小しても `expected` は 5 のまま変わらない．
  → **登録 2 台では `ready_count` が理論上も絶対に 5 に届かず，実験は永久に開始しない**（デッドロック．
  クラッシュではなく無限待機のため，コンテナは正常稼働に見えるが実質ハング）．
- `results/` に `Iter12ctrl_*` 相当のディレクトリは存在しない（`_wait_for_ready()` 内で `all_ready` になった
  時点で初めて実験ディレクトリを作成するため，未到達＝ディレクトリ未作成．データ欠損なし，やり直しに支障なし）．

**対応（本セッションで実施）**
- ハングしたコンテナ 3 つ（`wafl-peft-server`／`wafl-peft-client-0`／`wafl-peft-client-1`）を `docker stop` +
  `docker rm` で停止・削除した（`registry` コンテナ・イメージ・キャッシュ・`.venv` 等には一切触れていない．
  `mise run clean` 等の破壊的操作は実行していない）．このまま 80 分のタイムアウトまで放置しても実験が
  開始しないことが構造的に確定していたため，重複起動やタイムアウト浪費を避ける目的で停止した．
- **control（非同期）・treatment（同期バリア）とも未実行**．上記ブロッカーが解消しない限り，`hosts.txt` を
  2 台のままでは新規実行しても同じデッドロックを再現するのみと判断し，実行を見送った．

**判断が必要な選択肢（本 subagent の権限を超えるため実行せず提示）**
- B1: `.100/.102/.103` の外部競合が解消する（他ユーザーのジョブ終了）まで待ち，5 台構成のまま再試行する．
  Iter1〜11 の 5 ノード baseline と直接比較可能だが，再開時期が不確定．
- B2: 2 台構成用に `data/contact_pattern/` を新規生成（`mise run setup:contact-pattern` 相当，peer 数変更）し，
  `config/settings.json` の `contact_pattern_file` を切り替えて 2 台で実験を継続する．即座に着手できるが，
  時変トポロジー・接触頻度が変わり Iter1〜11 の 5 ノード結果と比較不能になる（単一レバー原則から逸脱し，
  P2P 同期方式とノード数の 2 変数が同時に変わる）．
- いずれも設定ファイル形式・実験スコープに関わる変更のため，CLAUDE.md の規約に従いユーザー確認を待つ．
  本 subagent からは journal 記録とブロッカーの事実報告のみ行い，どちらを選ぶかの判断はしていない．

---

## Iteration 11（Iter11_20260712T163256, 実行済み）: 容量増 rank32 → 逆効果・棄却
- 単一レバー: LoRA rank 16→32 / alpha 32→64．他は Iter10 最良構成に固定．
- 結果: ノード別 accuracy 平均 +5.2pt（最終 16.2%）で Iter10（+14.0pt/22.5%）より大幅悪化．last≪peak．
- 判定: **棄却・収束**．余分な容量が小シャードを強く過学習させ終盤で汎化破綻．~22% は容量不足でなく
  model+task+データ量の制約．rank16 へ復帰．容量レバーは収束と結論．

## Iteration 10（Iter10_20260712T150353, 実行済み）: dropout 0.15 → 僅かに最良・採用
- 単一レバー: LoRA dropout 0.10→0.15．
- 結果: ノード別 +14.0pt（最終 22.5%），マージ 12.5→20.0%．高速ノードの過学習解消（peak≈last）．
- 判定: **採用**．ただし Iter8(21.5%)→9(22.0%)→10(22.5%) はノイズ（±5pt）範囲で，dropout レバーは収束．

## Iteration 9（default_20260712T132221, 実行済み）: gentle LR 減衰 → 有益・採用
- 単一レバー: lr_min_ratio 1.0→0.5（終盤に base の 50% へ緩やか減衰）．
- 結果: ノード別 +12.5pt（最終 22.0%），マージ 12.5→25.0%（最良）．高速ノードの過学習緩和．
- 判定: **採用**．過剰減衰（Iter2 の 0.1）は逆効果だが 0.5 は有益．

## Iteration 8（Iter8_20260712T114410, 実行済み）: VRAM 削減で外部競合下 OOM 解消
- 変更: paged 8bit AdamW + chunked cross-entropy + seq256→208 + expandable_segments．
  （外部 GPU 競合増大による OOM への対処．単一レバーというより基盤対策）
- 結果: 全 5 ノード OOM なく完了．ノード別 +13.5pt（最終 21.5%）．Iter5 と同等＝VRAM 削減も seq208 も品質を損なわず．

## Iteration 7（3000s + cosine, 序盤で中止）
- window 3000s + lr_min 0.1 で起動したが，3000s は Iter6 で過学習と判明済みのため序盤で中止し 1500s へ仕切り直し．

## Iteration 6（default_20260712T003417, 実行済み）: 学習ウィンドウ倍増 → 過学習露呈・棄却
- 単一レバー: 接触パターン n_time 1500→3000（学習ウィンドウ倍増）．
- 結果: ノード別 +8.0pt（最終 17.8%）で Iter5 より悪化（peak は 23-30% と高いが last が大きく下回る）．
  マージは 20→27.5% と改善（過学習した多様な LoRA の平均化が正則化として機能）．
- 判定: **棄却**．「もっと訓練」は上限を上げず最終性能を悪化．1500s が最適．

## Iteration 5（default_20260711T225845, 実行済み）: grad_accum 8 再測定・評価 80 サンプル化
- 単一レバー: grad_accum 4→8．post-eval を 40→80 サンプルへ（ノイズ ±7%→±5%）．
- 結果: ノード別 +13.8pt（最終 21.0%）．ga8 ≈ ga4（差はノイズ範囲）．以降 ga8 採用．~20-26% 上限の存在を観察．

## Iteration 4（default_20260711T214544, 実行済み）: P2P スロットル修正反映・これまで最良
- 変更: P2P 送信の過剰シリアライズ修正（shadow_version 導入で GIL 競合解消）．設定は Iter3 と同じ．
- 結果: ノード別 +14.0pt（最終 20.0%）．step 時間が 0.6-0.7s に均一化（以前の 7s スパイク消滅）．

## Iteration 3（default_20260711T204215, 実行済み）: Iter1 勝ち設定へリバート + 評価信頼性向上
- 単一レバー: grad_accum 8→4．post-eval samples 20→40, checkpoints 8→6（評価信頼性）．
- 結果: ノード別 +9.5pt（最終 16.5%, 40 サンプル）．Iter1 の +16pt は 20 サンプルのノイズ込みで，真の改善は +9〜10pt が妥当と判明．

## Iteration 2（default_20260711T194846, 実行済み）: cosine 減衰追加 → 回帰・棄却
- 単一レバー: lr 2e-4→3e-4 + 時間ベース cosine 減衰（x0.1 まで）．
- 結果: ノード別 +7.0pt（最終 14.0%）で Iter1 より悪化．
- 判定: **棄却**．26 分・~160 更新の訓練不足領域では LR 早期減衰が不利．定数 LR が良い．accuracy は 20 サンプルで ±10pt ノイズと判明．

## Iteration 1（default_20260711T185433, attempt2 で成功）: 勾配累積 + シャッフル + LR warmup + 均等化
- 単一レバー群（初期実装）: lr 1e-4→2e-4, grad_accum 8, warmup 20, データシャッフル, シャード均等化 ~1345, max_seq_len 320→256．
- attempt1 は OOM（累積境界のみメモリ解放にした実装ミス + 外部競合）→ 毎 micro-step 解放 & seq256 で解消．
- 結果: ノード別 +16.0pt（0→25 等），baseline +6.0pt を大きく上回る．マージ 15→25%．Average loss 0.515．
- 判定: **採用**．学習効率・データ規模拡大の両面で baseline を上回る．

## Baseline（default_20260711T164008）
- 設定: lr 1e-4, batch=1（勾配累積なし）, シャッフルなし, 分割不均衡（335〜2606）, max_seq_len 320．
- 結果: ノード別 +6.0pt（最終 10〜25%）, Average loss 0.458．
