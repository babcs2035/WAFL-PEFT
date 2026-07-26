# d0001: WAFL-PEFT 文献調査・実測記録（2026-07-26）

Iter1〜12 の結果を再検討するために実施した文献調査（tavily）と，リポジトリに対する実測の**完全な記録**である．
意思決定と提案は `plans/p0001_research_direction_2026-07.md` にまとめてあり，本ファイルはその根拠となる
一次情報を出典付きで残すことを目的とする．

**表記規約**: 「論文は〜と報告している」は出典の主張，「〜と考えられる／推測される」は本調査での解釈である．
この 2 つを混同しないこと．

---

## 第 1 部: リポジトリに対する実測（文献ではなく本調査の測定結果）

### 1.1 GSM8K のトークン長分布と `max_seq_len` による切り詰め

`cache/models/google/gemma-4-E2B` のトークナイザーと `cache/datasets/gsm8k/main/train-00000-of-00001.parquet`
（7,473 件）を用い，`src/client.py:395-397` と同一の前処理（`prompt = "Question: {q}\nAnswer:"`，
`full_text = prompt + " " + answer`）を再現して測定した．

**トークン長分布**

| 対象 | 平均 | 中央値 | p75 | p90 | p95 | p99 | 最大 |
|---|---:|---:|---:|---:|---:|---:|---:|
| full_text（prompt + answer） | 188.1 | 175 | 225 | 282 | 319 | 397 | 543 |
| prompt のみ | 64.9 | 61 | — | — | 105 | — | 218 |
| answer のみ（CoT + `#### N`） | 123.3 | 112 | — | — | 230 | — | — |

**`max_seq_len` 別の切り詰め率**

| max_seq_len | 完全切り詰め（除外される） | 部分切り詰め（回答末尾が欠落） | 無傷 |
|---:|---:|---:|---:|
| **208（現行）** | 1 (0.0%) | **2,428 (32.5%)** | 5,044 (67.5%) |
| 256 | 0 | 1,118 (15.0%) | 6,355 (85.0%) |
| 320 | 0 | 364 (4.9%) | 7,109 (95.1%) |
| 384 | 0 | 93 (1.2%) | 7,380 (98.8%) |
| 512 | 0 | 3 (0.0%) | 7,470 (100.0%) |
| 640 | 0 | 0 (0.0%) | 7,473 (100.0%) |

208 では平均 59.3 トークン（最大 335）が失われ，**全 answer トークンの 15.6% が欠落**する．
GSM8K の `#### N` は解答の**末尾**にあるため，切り詰められた 2,428 件は構造上 100% が最終回答行を失う．

**コード側の挙動**: `src/client.py:414` の `skipped_fully_truncated` は
`prompt_len >= len(full_ids)` の場合のみカウントするため該当は 1 件のみで，
**上記 32.5% は検出されずそのまま学習に使われる**．評価側 `src/gsm8k_eval.py:56` は
生成文に `####` が含まれるかで採点するため，学習と評価の要求形式が食い違っている．

**文献側の裏付け**: MetaMath の公式 SFT 設定は GSM8K+MATH に対し **Max length 512**
（https://huggingface.co/meta-math/MetaMath-7B-V1.0 ）．本測定と整合する．

### 1.2 モデル重みの内訳（PLE が非量子化で残る問題）

`cache/models/google/gemma-4-E2B/model.safetensors`（10,246,621,918 バイト）のヘッダを直接読み，
テンソル単位のサイズを実測した．

| テンソル | 形状 | dtype | サイズ |
|---|---|---|---:|
| `model.language_model.embed_tokens_per_layer.weight` | [262144, 8960] | BF16 | **4.375 GiB** |
| `model.language_model.embed_tokens.weight` | [262144, 1536] | BF16 | 0.750 GiB |
| `model.language_model.layers.15.mlp.down_proj.weight` | [1536, 12288] | BF16 | 0.035 GiB |
| （以下 Linear 層はいずれも 1 層 0.035 GiB 程度） | | | |
| **embed 系合計** | | | **5.162 GiB** |
| **ファイル全体** | | | **9.543 GiB** |

`config.json` は `model_type: gemma4`, `architectures: [Gemma4ForConditionalGeneration]`．
モデルカードによれば Gemma 4 E2B は実効 2.3B / 埋め込み込み 5.1B である．

**帰結**: bitsandbytes が量子化するのは `nn.Linear` のみで `nn.Embedding` は量子化しない．
したがって **5.162 GiB（全体の 54%）は 4-bit 化されず fp16 のまま VRAM に常駐する**．
量子化対象の Linear 部（約 4.4 GiB）が NF4 で約 1.2 GiB になっても，重み合計は約 6.4 GiB になる．
`src/client.py:289` の「4-bit で約 2.5 GiB」というコメントは PLE を勘定していない．

`README.md` L205-213 に記録された経緯（外部ジョブ増でピーク約 9.5GB が 12GB を超え全ノード OOM →
paged 8-bit AdamW + chunked CE + `max_seq_len=208` でピーク約 8.85GB へ）は，この構造で説明できる．

### 1.3 PT 版と IT 版の食い違い

- `tokenizer_config.json` に `chat_template` キーが**無い**（`jq 'has("chat_template")'` → `false`）
- `chat_template.jinja` も存在しない
- → デプロイされているのは **PT（base）版**である
- 一方 `README.md` は `gemma-4-E2B-it` を **4 箇所**で参照している
- `config/settings.json` の `model_id` は `google/gemma-4-E2B`（PT）

どちらが意図なのかを確認する必要がある．

### 1.4 マージ実装が WAFL の定式化から乖離している

`src/client.py:882-902`:

```python
merged: dict[str, torch.Tensor] | None = None
count = 0
for weight_bytes in buffers_to_merge.values():   # ← remote peer のみ
    remote_weights = _deserialize_weights(weight_bytes)
    if merged is None:
        merged = {k: v.float() for k, v in remote_weights.items()}
        count = 1
    else:
        for k in merged:
            if k in remote_weights:
                merged[k] += remote_weights[k].float()
        count += 1
if merged is not None and count > 0:
    for k in merged:
        merged[k] /= count                        # ← 自ノードは分母に入らない
```

`src/client.py:1151-1155`:

```python
if merged_weights is not None:
    with torch.no_grad():
        for name, param in model.named_parameters():
            if name in merged_weights:
                param.copy_(merged_weights[name].to(param.dtype).to(param.device))  # ← 完全上書き
```

**接触相手が 1 台の場合（RWP のペア接触では通常こうなる），これは平均ではなく「相手の重みへの置換」であり，
直前のマージ以降に自ノードが行った学習はすべて破棄される．**
WAFL 原典（arXiv:2205.11779）の定式化は自ノードを含む平均であり，本実装は別アルゴリズムになっている．
ADF-LoRA（arXiv:2511.18291）も接触ペアの集約を "symmetric LoRA averaging" と記述している．

### 1.5 LoRA A の初期値がノード毎に異なる

- `src/client.py:167`: `seed = base_seed + peer_id`
- `src/client.py:1523`: `set_deterministic_seed(PEER_ID)`
- `src/client.py:355`: `torch.nn.init.kaiming_uniform_(new_data, a=math.sqrt(5))`（`lora_A`．`lora_B` はゼロ初期化）

各ノードの A が独立な乱数行列になる．中央集権 FL では毎ラウンド同一の A が配布されるためこの状況は生じない．
独立な kaiming 行列を n 個平均すると振幅が概ね 1/√n に縮む（数学的帰結であり文献の主張ではない）．

### 1.6 接触パターンの実測（`src/generate_contact_pattern.py` による）

n_time=600, a=500, r=100, p=10, v∈[1,5], seed 42/43/44 で生成した場合:

| 指標 | n=5 | n=10 |
|---|---|---|
| ペアあたり接触イベント数 | 1.7–2.2 | 1.5–1.8（ほぼ不変） |
| ノードあたり接触イベント数 | 6.8–8.8 | 13.8–15.8（約 2 倍） |
| 平均次数 | 0.61–0.77 | 1.12–1.22（約 2 倍） |
| 600 step 中一度も会わないペア | 0–10% | 4–16%（悪化） |
| 瞬時グラフが連結な時刻の割合 | ≈0 | ≈0 |

現存する接触パターンファイルは `data/contact_pattern/rwp_n05_a0500_r100_p10_s42.json` の 1 本のみ
（n=5，seed 42 のみ）．

---

## 第 2 部: 統計的検出力（評価方法論）

### 2.1 二項分布に基づく検出力の計算

accuracy p ≈ 0.225 における二項標準誤差 SE = √(p(1−p)/n):

| n | SE | 95% CI 半幅 | 2 条件差の SE | 80% power の最小検出可能差 |
|---:|---:|---:|---:|---:|
| 40 | 6.60pt | ±12.9pt | 9.34pt | **26.2pt** |
| 80 | 4.67pt | ±9.2pt | 6.60pt | **18.5pt** |
| 250 | 2.64pt | ±5.2pt | 3.73pt | 10.5pt |
| 1319（GSM8K test 全体） | 1.15pt | ±2.3pt | 1.63pt | 4.6pt |

Wilson 95% CI: 18/80 = 22.5% → **[14.7%, 32.8%]**（幅 18.1pt）．n=40 では [12.3%, 37.5%]（幅 25.2pt）．

`config.yml` の旧成功条件「ノイズ（±5pt）を超えて +2pt 以上」を 80% power・α=0.05 で満たすには，
独立比較で 1 群あたり **6,843 問**，同一問題での対比較（McNemar，不一致率 ψ=0.1 想定）でも
**約 1,962 問**が必要である．GSM8K test 全体（1,319 問）でも足りない．

### 2.2 既存の判定の検算

| 判定 | 差 | n=80 での z | p 値 | 評価 |
|---|---:|---:|---:|---|
| ベスト構成 vs 孤立学習（8.5% → 22.5%） | +14pt | 2.49 | ≈0.013 | 辛うじて有意 |
| ノード別 22.5% vs マージ 25.0% | +2.5pt | 0.37 | ≈0.71 | **ノイズと区別できない** |
| Iter11 rank32（22.5% → 16.2%） | −6.3pt | ≈1.0 | — | **「大幅悪化」とは言えず棄却根拠不足** |
| Iter6 窓 3000s，Iter9 lr_min_ratio，Iter10 dropout | 微差 | — | — | **すべて識別不能** |

**結論: +14pt の全体効果を除き，Iter1〜12 のほぼすべての判定が測定ノイズの範囲内である．
「小シャード由来の過学習限界で収束」という Iter11 時点の判定は，データが示した結論ではなく
評価解像度の不足によって差が見えなかっただけである可能性が高い．**

### 2.3 出典

- Bowyer, Aitchison, Ivanova, *Position: Don't Use the CLT in LLM Evals With Fewer Than a Few Hundred
  Datapoints*, ICML 2025 Spotlight, arXiv:2503.01747 — n < 数百 では CLT ベースの誤差棒が不確実性を
  大幅に過小評価すると示す．
- Miller (Anthropic), *Adding Error Bars to Evals*, arXiv:2411.00640
  （https://arxiv.org/pdf/2411.00640 ，解説: https://www.anthropic.com/research/statistical-approach-to-model-evals ）
  — 「3pt の差を 80% power で検出するには約 969 問必要」「新しい eval は最低 1000 問持つべき」
  「対応のある比較を使う」と推奨．
- README.md 自身が「40 サンプルでもノイズは ±5〜7pt」と記録している（本研究内の記述）．

---

## 第 3 部: 分散環境における LoRA 集約

### 3.1 中核となる問題

LoRA の更新は BA という積の形なので，A と B を独立に平均すると本来の集約とずれる
（`avg(B)·avg(A) ≠ avg(B·A)`）．この差分を cross-term noise と呼ぶ．
**WAFL は接触ごとに集約するため，中央集権 FL の「ラウンド」より遥かに多い回数この誤差が累積する**
（この含意は本調査の解釈）．

### 3.2 手法一覧（報告値は論文の主張をそのまま記載）

| 手法 | 出典 | 中核アイデア | 報告された改善幅 | 実装コスト | WAFL-PEFT 適合性 |
|---|---|---|---|---|---|
| **FedIT**（素の FedAvg） | Zhang+ ICASSP 2024 | A と B を独立に平均 | 基準 | — | **現行実装がこれに相当** |
| **FFA-LoRA** | Sun+ ICLR 2024, arXiv:2403.12313（[PDF](https://proceedings.iclr.cc/paper_files/paper/2024/file/4e243e95c913b367775d71d7182b99d9-Paper-Conference.pdf)） | A を乱数初期値で凍結し B のみ学習・集約．通信半減 | **設定依存で符号が反転する**．50 client/rank4 の GLUE 平均 70.72 → **76.48（+5.76pt）**，rank8/50client 64.03 → 72.46（+8.43pt）（RoLoRA 論文 Table 1）．一方 3 client では 88.20 → 87.48（−0.72pt）．commonsense（Llama-3.2 3B）は FedIT 83.57 → 77.35（**−6.2pt**，FedEx-LoRA 論文） | 設定 1 つ相当 + 数行 | 小シャード・多ノードという条件は合致するが，**現行コードは A の初期値がノード毎に異なるため前提が崩れる**（§1.5） |
| **FedSA-LoRA** | Guo+ ICLR 2025, arXiv:2410.01463（https://arxiv.org/html/2410.01463v3 ） | A・B とも学習するが **A のみ共有**．A=汎用知識，B=クライアント固有知識という非対称性の分析に基づく | GLUE(RoBERTa-large, 3client, Dir0.5) 平均 89.33 → 90.48．**GSM8K(LLaMA3-8B, 3client, IID): LoRA 46.23 → FFA 46.32 → FedSA 46.63（+0.40pt）**．CodeSearchNet non-IID 58.34 → 59.66 | 集約関数に 10 行未満 | 高い．B を local に残すのは per-node accuracy 指標と整合的．ただし GSM8K の報告改善は +0.40pt と小さい |
| **RoLoRA** | Chen+ NeurIPS 2025, arXiv:2502.01755（https://arxiv.org/html/2502.01755v4 ） | ラウンド毎に B 相 / A 相を交互に切替，その相で学習中のブロックのみ集約．同一相では相手ブロックが共通なので cross-term が消える | **20client: 78.90 → 87.03（+8.13pt），50client: 70.72 → 85.81（+15.09pt），rank8/50client: 64.03 → 86.27（+22.24pt）**．Dir(0.5)/10client MNLI 81.19 → 82.60，Dir(1.0)/15client MNLI 74.54 → 81.55．通信は LoRA の半分 | 20〜30 行 | **数値は最も魅力的だが，DFL では劣化する（後述）** |
| **ADF-LoRA** | arXiv:2511.18291（2025-11，https://arxiv.org/pdf/2511.18291 ） | RoLoRA を DFL 化．学習は片ブロックのみだが，**混合（平均）は A・B 両方**行い凍結ブロックの drift を防ぐ | **10client・ペア接触確率 0.1/round という WAFL 相当の設定**で LoRA 0.8458 / FFA 0.8268 / RoLoRA 0.8284 / **ADF-LoRA(T=5) 0.8505**．MNLI 0.7304 → 0.7624（+3.2pt）．T ablation: T=1:0.8435, 5:0.8505, 10:0.8393, 20:0.8421 | 20〜30 行 | **最も近い設定．ただし平均改善は +0.47pt** |
| **TAD-LoRA** | arXiv:2602.00451（2026-02，https://arxiv.org/html/2602.00451v1 ） | ADF の理論版．最適切替間隔 T\*(ρ) ≃ Θ(1/√(1−ρ))．疎な接続ほど大きい T が良い | ER グラフ p=0.5: LoRA .8754 / TAD .8731（改善なし），p=0.1: .8375 → .8418，**p=0.02: .7798 → .8032（+2.34pt）** | 20〜30 行 | 接触が疎なほど効く．RWP の接触密度次第 |
| **FedEx-LoRA** | Singhal+ ACL 2025 Oral, arXiv:2410.09432（[ACL PDF](https://aclanthology.org/2025.acl-long.67.pdf) ） | 残差 ΔW_res = avg(BᵢAᵢ) − avg(B)avg(A) を**凍結ベース重み W0 に加算**して厳密集約 | **GSM8K(Mistral-7B, 3client): FedIT 56.94 → 62.62（+5.68pt，centralized skyline 62.77）**．GSM8K(Gemma-2 9B): 74.57 → 76.19．commonsense 平均 FedIT 比 +2.42%，FFA 比 +8.63% | 大規模な書き換え | **低い．4-bit 量子化ベースに残差を足せない**（dequant→加算→requant が必要．空き VRAM では非現実的） |
| **FLoRA** | Wang+ NeurIPS 2024, arXiv:2409.05976 | 各 client の A,B をブロック対角に stack．厳密かつ異種 rank 対応 | LoRA-FAIR 論文の ViT/DomainNet では FedIT 75.75 に対し 75.53（改善なし）．RoLoRA 論文は「RoLoRA が一貫して上回る」と報告 | 大規模 | 低い．adapter サイズがノード数に比例 |
| **FlexLoRA** | Bai+ NeurIPS 2024 | ΔWᵢ を足して truncated SVD で再分解 | LoRA-FAIR の ViT: 75.75 → 76.02．**RoLoRA 論文では 50client で 54.67（LoRA 70.72 より大幅に悪化）** | 集約関数に 30 行程度 | 低い．多ノードで崩壊すると報告あり |
| **LoRA-FAIR** | Bian+ ICCV 2025, arXiv:2411.14961（https://arxiv.org/html/2411.14961v1 ） | サーバ側で B̄+ΔB を最適化し，理想更新への近さと初期化整合性を両立 | ViT/DomainNet: FedIT 75.75 → 77.07（+1.32pt）．MLP-Mixer 64.37 → 65.87．NICO++ 90.58 → 91.24 | 集約関数に 30 行 + 最適化 | 中．**検証は ViT/MLP-Mixer のみで LLM 実験なし**．"Client-Side Initialization Drift" として初期化整合性の重要性を指摘 |
| **Dec-LoRA** | Ghiasvand+ REALM@ACL 2025（[PDF](https://aclanthology.org/2025.realm-1.24.pdf) ） | cross-term 対策なしの素の gossip LoRA．DFL のベースライン | 「DFL ≈ CFL」．RoBERTa-base 10client ring は centralized とほぼ同等．**4-bit 量子化でも劣化しない（QNLI 90.79 → 90.84，SST2 93.92 → 93.92）** | — | 参照値として有用．「QLoRA が分散を壊す訳ではない」根拠 |
| **SFed-LoRA** | arXiv:2603.08058（https://arxiv.org/html/2603.08058v1 ） | 集約時の統計分散が**クライアント数 N に比例して増大**し高 rank で gradient collapse を起こすと主張．最適スケーリング因子 `γ = α√(N/r)` を導出 | — | — | rank16/alpha32 を固定したまま N を 5→10 にすると実効スケールがずれる可能性．**ただし同論文はサーバ集約型 FedSA-LoRA 前提であり，WAFL のペアワイズ平均への適用は未検証** |

### 3.3 重要な反証

- **RoLoRA の +15pt は DFL では再現していない．** ADF-LoRA 論文の DFL 表では RoLoRA 0.8284 < 素の LoRA
  0.8458 と**劣化**する．RoLoRA の大幅改善は「サーバが凍結ブロックの同一性を強制できる」前提に依存する．
  WAFL にはサーバがないのでそのまま持ち込むと逆効果になりうる．TAD-LoRA も同じ指摘をしている．
- **DFL 特化手法の改善幅は小さい．** ADF-LoRA の平均改善は +0.47pt，TAD-LoRA は p=0.5 で改善なし・
  p=0.02 で +2.34pt．既存実験の +14pt という変動幅から見ると，接触が十分密なら測定ノイズに埋もれる．
- **すべての参照実験がタスク・モデル規模で乖離している．** DFL 系（ADF/TAD）は RoBERTa-Large 335M の
  GLUE 分類であり，2B 級モデルの GSM8K 生成タスクではない．FedSA-LoRA の GSM8K は 3client・IID・
  LLaMA3-8B である．**1,345 サンプルという極小シャードで cross-term 対策が効くかを検証した論文は
  今回の調査範囲では見つからなかった．**
- **WAFL（接触ベース・時変トポロジー）× LoRA/PEFT を直接扱った論文は，今回の検索範囲では
  見つからなかった．** ADF-LoRA の「ペア接触確率 0.1」設定が最も近い．新規性の余地であると同時に，
  「先行研究の数値を根拠に効果を予測できない」ことも意味する．

---

## 第 4 部: WAFL 原典と評価軸・ノード数スケール

### 4.1 WAFL 原典

Ochiai, Sun, Jin, Wongwiwatchai, Esaki, *Wireless Ad Hoc Federated Learning*,
arXiv:2205.11779（https://arxiv.org/pdf/2205.11779 ．IEEE TNNLS 投稿版）．
プロジェクト: https://github.com/jo2lxq/wafl

- MNIST + MLP，90% Non-IID，**10 ノード固定**（1 ノード約 4,700〜5,400 サンプル）
- 静的 4 トポロジー（line/tree/ringstar/full-mesh）と動的 2 種（RWP，Community-Structured Environment）
- RWP は `rwp0500 / rwp1000 / rwp2000` の 3 種で，**数字は移動領域の一辺 [m]**．
  無線到達 100m，pose 10 epoch，速度 [3.0, 7.0] m/epoch
- 結果: self-train 84.7% に対し WAFL 94.8〜96.3%．
  rwp0500=95.389 / rwp1000=95.230 / rwp2000=93.659（λ=1.0, η=0.001）で，**接触が疎になるほど劣化**
- **すべてシミュレーション**（"carried out the experiment by simulation on a single computer" と明記）

本プロジェクトの `rwp_n05_a0500_r100_p10_s42.json` は a=500/r=100/p=10 と原典 rwp0500 に一致するが
**n=5** である（速度既定は [1.0, 5.0] で原典と異なる．`src/generate_contact_pattern.py`）．
つまり原典の作法は「n を 10 に固定し，面積（接触密度）を振る」であり，n 自体をレバーにした実験は
行っていない．**10 ノード化は原典設定への復帰である．**

原典は Eq.(4) 直後に過学習対策を明記している:

> "This adjustment process should be carried out only if |nbr(n)| > 0 ... **If |nbr(n)| = 0, this
> minibatch-based adjustment process should be skipped because it causes over-fitting to the local
> dataset**"

さらに "this self-training should not be carried out after starting the model exchange phase" とも述べる．

### 4.2 WAFL 系の評価軸と本研究の空白

| 評価軸 | 先行研究での扱い | 本研究の状況 |
|---|---|---|
| IID テストセットでの accuracy | 原典の主軸．全後続論文が採用 | 実施済（ただし n=40〜80） |
| epoch 数ベースの収束速度 | MemWAFL (Eguchi+, FNWF 2023) が「sparse dynamic network での精度と速度」を主張 | 部分的 |
| 通信量 | Tsuchiya+, IEEE CAI 2025「Top-K Difference Sparsification and Quantization」が正面から扱う | **未測定** |
| トポロジー依存性 | 原典は定性的（line/tree/RWP 等の比較）．spectral gap 等の定量指標は**使っていない** | **未測定．かつ先行研究も空白** |
| モデルパラメータの収束（層ごとの誤差軌跡） | 原典 Fig.（fc1.weight 等の error 曲線） | 未測定 |
| スケーラビリティ（ノード数） | Ito+, FNWF 2023「Self-Organizing Hierarchical Topology」（IEEE Xplore ペイウォールのため検証ノード数を確認できず） | 未実施 |
| 不均一な計算進捗・staleness | **WAFL 系では見当たらない**．非同期 gossip 側（Dutta+ の error floor，SA-ADFL）にはある | **未測定（空白）** |
| 実機 wall-clock throughput | **WAFL 系はすべてシミュレーション** | **実機 10 台という本研究固有の強み** |
| 生成系 LLM / PEFT への適用 | Ueda+, IEEE CAI 2025（VQA）が最も近い．LLM の推論タスク + LoRA は見当たらない | **本研究の位置** |
| 統計的誤差棒 | WAFL 系は単一曲線のみ | 未実施 |

**空白の特定**: 「実機での wall-clock throughput × 不均一な計算進捗 × トポロジーの定量化」の 3 点が，
WAFL 系・本研究の双方で未測定である．

### 4.3 ノード数スケールの文献

文献の圧倒的多数は「**総データ量固定**」で比較している．

- arXiv:2503.20768 §6（https://arxiv.org/html/2503.20768v1 ）は
  "**By maintaining the total dataset size and varying the number of clients**" と明言．
  画像分類で 10→100→1000 クライアントの fast-learnt accuracy は 49.5 → 32.1 → 21.6%．
  ただし「長期では 10 と 100 が同じ 70% に収束」とも報告．
- arXiv:2504.08198（https://arxiv.org/html/2504.08198v1 ）は CIFAR-10 を K∈{5,10,20,50,100} に分割し
  5→100 で約 30pt 低下．原因を「クライアントが受け取るデータが遥かに少なくなるため」と明示．
  Table I: Li 10→40 で 0.68→0.60，Song 10→100 で 0.503→0.414，Xu 100→500 で 0.5578→0.3378．
  一方 Zhang 100→300 は 0.575→0.645 と増加で，**結論は一致していない**．

### 4.4 分散 SGD の収束理論

Koloskova+ ICML 2020, *A Unified Theory of Decentralized SGD with Changing Topology*
（https://proceedings.mlr.press/v119/koloskova20a/koloskova20a.pdf ）の強凸レート:

```
Õ( σ̄²/(nμT) + L(τ²ζ̄² + τp·σ̄²)/(μ²p²T²) + LτR₀²/p · exp(−μTp/(τL)) )
```

- 第 1 項が **n に対する linear speedup** を与える．同論文は「ノイズが高いときトポロジー・局所ステップ数・
  データ異質性への依存は弱い」と結論する．**ただしこの speedup は各反復で各ワーカーが勾配を計算する前提
  （総計算量が n に比例して増える設定）であり，総データ量固定・総ステップ固定では成立しない**
  （この但し書きは本調査の解釈であり論文の主張ではない）．
- 同時に mixing parameter p に 1/p²・1/p で依存し，「ring と torus の反復数比は p の比 ≒ Θ(n)」と述べる．
  また "when the noise is small (e.g. large mini-batches), the effect of those parameters become more
  pronounced"．
- de Vos+ *Epidemic Learning*, NeurIPS 2023
  （https://proceedings.neurips.cc/paper_files/paper/2023/file/7172e147d916eef4cb1eb30016ce725f-Paper-Conference.pdf ）
  は，漸近線形加速に到達するまでの transient iterations が **O(n³/s²)** であることを示す．
  n が増えると過渡期が急速に伸びる．
- Vogels+ NeurIPS 2022, *Beyond spectral gap*
  （https://proceedings.neurips.cc/paper_files/paper/2022/file/61162d94822d468ee6e92803340f2040-Paper-Conference.pdf ）
  は「**spectral gap は現実的にチューニングされた学習率下では性能を予測しない**」と実験的に示す．
  説明変数としては使えるが予測指標として信頼してはならない．
- Charles+ *On Large-Cohort Training*, NeurIPS 2021
  （https://proceedings.neurips.cc/paper/2021/file/ab9ebd57177b5106ad7879f0896685d4-Paper.pdf ）

### 4.5 Random Waypoint の接触率

Groenevelt らのペア間 meeting rate は `β ≈ 2·w·d·E[V*]/A`
（https://bpb-us-w1.wpmucdn.com/sites.usc.edu/dist/b/364/files/2019/05/EE650_epidemicmodeling.pdf ，
Zhang+ 経由 http://www-sop.inria.fr/members/Giovanni.Neglia/publications/Zhang05_epidemicmodeling.pdf ）で，
**n に依存しない**．したがって面積・半径・速度を固定すればペアあたり接触率は不変，
ノードあたり接触率は (n−1) に比例する．§1.6 の実測はこの理論どおりの挙動を示している．

ノードあたり接触率を n=5 に合わせたいなら，理論上 A ∝ (n−1) なので n=10 では約 750m 四方になる．

---

## 第 5 部: 小データ過学習への対処

| 方向 | 出典 | 中核 | 期待 | コスト |
|---|---|---|---|---|
| **孤立時の局所学習スキップ** | WAFL 原典 Eq.(4) 直後（§4.1 に引用） | 近傍 0 のとき minibatch 調整を行わない | 原典自身が示す過学習対策．窓 1500s→3000s の悪化も「接触のない時間帯の局所学習増」で説明できる | 設定変更〜小 |
| 近接項（decentralized FedProx） | Li+ FedProx（収束解析 arXiv:2206.05187），Federated Residual LoRA ICLR 2025（[PDF](https://proceedings.iclr.cc/paper_files/paper/2025/file/906c860f1b7515a8ffec02dcdac74048-Paper-Conference.pdf) ） | 近接項 μ でローカル発散を抑制 | **中〜低**．同論文は GSM8K で FedAvg 32.67 / FedProx 32.29 と報告しており，FedProx の優位は確認されていない | 小 |
| **RFT による局所データ拡張** | Yuan+ 2023 RFT（LLaMA-7B: SFT 35.9% → RFT 41.7%，複数モデル集約で 49.3%），DART-Math（https://tongyx361.github.io/assets/dart-math/paper-dart-math.pdf ） | 各ノードが自ノードの問題に複数推論経路をサンプリングし，正解に至ったものだけを追加学習データにする．**データはノード外に出ないのでプライバシ制約を破らない** | 大．1,345 → 数千への拡張は「小シャード限界」仮説への直接の反証実験になる | 中〜大 |
| spectral gap による定量化 | Koloskova+ 2020，Vogels+ 2022（反証あり），Groenevelt | 接触ファイルから時間平均 gossip 行列を作り 1−λ₂ を算出し accuracy と対応付ける | 説明変数としては有用．**予測指標としては Vogels+ の反証があり期待しすぎない** | 小（解析のみ） |

---

## 第 6 部: モデルとデータセットの選択肢

### 6.1 ベースライン 22.5% の妥当性への疑義

- Gemma-2 2B PT の GSM8K は 5-shot maj@1 で **23.9%**（https://ai.google.dev/gemma/docs/core/model_card_2 ）
- Gemma 3n E2B IT は MGSM 0-shot **53.1%**，MMLU 0-shot 60.1%，HiddenMath 27.7%
  （https://ai.google.dev/gemma/docs/gemma-3n/model_card ）
- ローカルの Gemma 4 E2B モデルカードでは IT 版が AIME 2026 37.5%，MMLU Pro 60.0%

**8.5% というベースラインはこの水準から見て低く，「+14pt の改善」ではなく
「壊れた状態からの部分回復」を測っている疑いがある．**疑うべき原因（優先順）:

1. `max_seq_len 208` による CoT 切り詰め（**§1.1 で実測確認済み．32.5%**）
2. VRAM 制約の本体である PLE の非量子化（**§1.2 で実測確認済み．54%**）
3. PT 版 / IT 版の取り違え（**§1.3 で食い違いを確認**）
4. 回答抽出の正規表現の不一致
5. 4-bit 量子化による劣化．IJCAI 2025 は Llama-3.2-1B で 4-bit により GSM8K **−25.3pt** を報告
   （https://www.ijcai.org/proceedings/2025/0902.pdf ）

### 6.2 モデル候補

| モデル | パラメータ | 4-bit QLoRA の重み目安 | 報告 GSM8K | 6GB 制約下の可否 | 出典 |
|---|---|---:|---:|---|---|
| Gemma 4 E2B（現行，PT） | 2.3B eff / 5.1B | 約 **6.4 GiB**（PLE 5.16 GiB が非量子化） | 非公表 | **不可**（seq 208 でようやく成立） | 実測（§1.2）+ ローカル model card |
| Qwen2.5-1.5B-Instruct | 1.54B | 約 1.1 GiB | **73.2** | 可（余裕大） | https://qwenlm.github.io/blog/qwen2.5-llm/ |
| Qwen2.5-3B-Instruct | 3.09B | 約 2.0 GiB | **86.7** | 可 | 同上 |
| Qwen2.5-Math-1.5B(-Instruct) | 1.54B | 約 1.1 GiB | 1.5B の正確値は未確認（7B base で GSM8K 91.6 / MATH 55.4） | 可 | https://arxiv.org/html/2409.12122v1 |
| Llama-3.2-1B-Instruct | 1.24B | 約 0.9 GiB | 44.4 (8-shot CoT) | 可 | https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct |
| Llama-3.2-3B-Instruct | 3.21B | 約 2.1 GiB | 77.7 (8-shot CoT) | 可 | 同上 |
| Llama-3.1-8B-Instruct | 8B | 約 5.5 GiB | 84.5 | 条件付き（seq 512 は厳しい） | 同上 |

活性化メモリの外部実測: RTX 4060 8GB での LoRA/QLoRA プロファイリングでは
**VRAM 6.2〜8.1GB の範囲で seq_len 2048 まで実行可能**と報告されている
（https://arxiv.org/html/2509.12229v1 ）．3B 級 + 4-bit + gradient checkpointing なら
512 トークンは 6GB に収まると判断できる材料になる．

### 6.3 データセット候補

| 名称 | サンプル数 | 報告 FT 後 GSM8K | 10×1345=13,450 を満たすか | ライセンス注意 | 出典 |
|---|---:|---:|---|---|---|
| GSM8K train（現行） | 7,473 | — | **不可** | MIT | ローカル実測 |
| **MetaMathQA** | 395K | LLaMA-2-7B **66.5** / Mistral-7B **77.7** | 可（29 倍） | GPT-3.5/4 生成の派生．商用可否要確認 | https://huggingface.co/meta-math/MetaMath-7B-V1.0 |
| MetaMathQA-40K | 40K | — | 可 | 同上 | HF |
| Orca-Math-200K | 約 200K | Mistral-7B ベースで **86.81** | 可 | Azure GPT-4-Turbo 生成．MSR-LA 系の商用制限 | https://arxiv.org/abs/2402.14830 |
| OpenMathInstruct-2 | **14M**（uniq 約 600K） | Llama3.1-8B で MATH +15.9pt | 可（要サブセット） | CC-BY-4.0 系（NVIDIA） | https://openreview.net/forum?id=mTCbq2QssD |
| DART-Math | 590K（VRT 対照 590K） | 難問偏重で OOD に強い | 可 | — | https://arxiv.org/abs/2407.13690 |
| MathInstruct | 225K | OpenFedLLM が FL 実験の math クライアントに採用 | 可 | — | https://arxiv.org/pdf/2402.06954 |

**推奨は MetaMathQA**．GSM8K train を rephrase / FOBAR / self-verification で拡張したものなので
**問題分布が現在と連続**し，過去 12 イテレーションとの比較可能性を最大限保てる．

### 6.4 分散 FL の LLM 標準ベンチマーク

- **OpenFedLLM**（arXiv:2402.06954, KDD'24，被引用 296，https://arxiv.org/pdf/2402.06954 ）:
  8 データセット（Alpaca 52K, Alpaca-GPT4 52K, FinGPT 77K, MedAlpaca 34K, Code-Alpaca 20K,
  **MathInstruct 225K**, UltraFeedback 等）と 7 FL baseline を提供．マルチドメイン協調実験は
  4 クライアント（general/math/code/finance）× 各 **5K サンプル**で，評価は
  **MT-Bench / GSM8K / HumanEval / FPB**．
- **FedLLM-Bench**（arXiv:2406.04845, NeurIPS'24）: Fed-Aya / Fed-ChatbotIT / Fed-WildChat /
  Fed-ChatbotPA の 4 データセットで，現実的な非 IID を「自然なクライアント分割」として提供．

**GSM8K は FL 文献でも標準的な評価軸として使われており，タスクを分類問題に変える必然性はない．**
ただし OpenFedLLM は各クライアント 5K サンプルであり，現状の 1,345 は 1/4 未満である．
協調効果を出したいなら「タスクを易しくする」より「1 ノードあたりのデータを増やす」方が文献の標準に近い．
分類タスクに逃げると MT-Bench や GSM8K を使う既存 FL 研究群との比較可能性を失う．

---

## 第 7 部: 未達の調査・積み残し

- Ito+ FNWF 2023「Self-Organizing Hierarchical Topology」（https://ieeexplore.ieee.org/document/10520530 ）
  は IEEE Xplore ペイウォールで本文を取得できず，検証ノード数を確認できなかった．
  抄録から確認できたのは「ノードをグループに分けて段階的に集約する Hierarchical Topology (HT) と
  Group Selection Algorithm (GSA) を提案」までである．
- Qwen2.5-Math-1.5B の GSM8K 正確値は未確認（7B base の値のみ確認）．
- MetaMathQA / Orca-Math の商用利用可否は未確認．
- PLE を CPU オフロードした場合の実効速度は未測定．
- `src/server.py` の `_wait_for_ready()` が接触パターンから `expected` を導出する箇所は，
  10 ノード化時に再確認が必要（Iter12 のデッドロックの原因箇所）．
