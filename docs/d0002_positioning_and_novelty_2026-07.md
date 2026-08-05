# d0002: WAFL-PEFT の位置付けと新規性の調査（2026-07-12）

本プロジェクトが WAFL 系譜のどこに位置し，何を新規性として主張できるかを整理した調査記録である．
先行研究サーベイ・実装すべき機能・期待される結果・新規性・応用可能性の 5 セクションからなる．
論文（`paper.tex`）の背景と展望を書く際の材料として使う．

**読む際の注意（2026-08-05 追記）**

- 本文は 2026-07-12 時点の調査であり，**Iter14 で判明した「P2P 重み交換が Iter13 まで一度も機能して
  いなかった」という事実（`.claude/research/journal.md` の Iter14 調査）を反映していない**．
  「時変トポロジー下で知識収束が起きている」という前提の記述は，Iter14 以降の実験で改めて確認する必要が
  ある．
- 「ThinkPad 50〜70 台」という規模は構想であり，実測は RTX 3060 の 10 ノード構成である．
- 統計的な扱い（評価問題数の不足）については後続の `d0001_literature_survey_2026-07.md` と
  `../plans/p0001_research_direction_2026-07.md` が優先する．

---

## TL;DR
- 本プロジェクト（時変トポロジー×ストールフリーPEFTをThinkPad 50〜70台・RTX 3060 10台の実機で実証）は、東京大学 落合秀也・江崎浩研究室が確立したWAFL系譜の最新フロンティア（中島幸太氏ら2026年のWAFL×LLM研究、Shahら2026年のエミュレーション基盤）を「離散ラウンド制シミュレーション/エミュレーション」から「実時間・実機・大規模」へ押し上げる正統な次の一手であり、新規性は十分に主張できる。
- 最大の差別化点は3つ——(1)実時間軸で計算と通信を完全オーバーラップさせる「ストールフリー」を4スレッド構成で実証、(2)50〜70台という物理P2Pの実機規模、(3)LoRA-PEFT×GSM8K数学推論という「知識収束」タスクの採用。既存のWAFL研究は主にMNIST/ViT級のシミュレーション・少数ノード（原論文は10ノード）に留まっており、実機大規模×LLM×リアルタイム非同期は空白領域。
- 推奨する主張の核は「シミュレーションでは観測できない実機由来のスループット平坦性（GPUアイドル率の最小化）とNon-IID知識収束の両立」。期待結果としてストール率の大幅低減・スループット変動係数の低さ、時変トポロジー下でもself-training比で明確なGSM8K精度向上を定量提示すべき。

## Key Findings

**1. WAFLの系譜と「中島氏ら(2026)」の位置づけ**
- WAFL（Wireless Ad-hoc Federated Learning）は落合秀也（東京大学）らが2022年に提唱した完全分散協調学習の枠組み（Ochiai, Sun, Jin, Wongwiwatchai, Esaki, arXiv:2205.11779, 後にIEEE TNNLS）。中央サーバーを持たず、無線範囲内で近接したノード同士が機会的接触（opportunistic contact）で局所モデルを交換・集約し、Non-IIDデータから汎化モデルを形成する。原論文の評価は「四つの静的通信ネットワーク＋Random Waypoint mobilityとcommunity-structured environmentに基づく二種の動的・機会的通信ネットワーク」を用い、90% Non-IID MNISTを10ノード（2層全結合NN）で学習。同論文アブストラクト（verbatim）では「WAFL has achieved higher accuracy of 94.8-96.3% than the self-training case of 84.7%」と報告されている（本文では94.7–96.2%表記）。
- 「中島氏ら(2026)の離散ラウンド制シミュレーションデータ」に相当する先行研究は、同研究室の中島幸太（Kota Nakajima）氏らによる2026年の会議論文「Serverless Cross-Device Knowledge Transfer with Collaborative LLM Fine-Tuning via Wireless Ad Hoc Federated Learning」および田中幹起（Motoki Tanaka）氏らの「Robust Training of Large Language Models under Non-IID Data in Wireless Ad Hoc Federated Learning」（いずれもEsaki・Ochiai研）と強く一致する。これらはWAFLをLLM協調ファインチューニングへ拡張した最新研究だが、WAFL標準の評価パラダイムはエポック/ラウンド単位でcontact_patternに沿って接触時のみモデル交換する「離散ラウンド制シミュレーション」である点が本プロジェクトの出発点と符合する。
- さらに直接の前身として、Namit Shah, Kosei Takahashi, Tatsumi Yamazaki, Natsuki Zenko, Hiroshi Esaki, Hideya Ochiai「An Emulation Platform for Wireless Ad Hoc Federated Learning: Design, Implementation, and Case Study」（KST 2026, pp.194-199, DOI 10.1109/KST67832.2026.11431966）が存在する。これはWAFLのエミュレーション基盤であり、本プロジェクトが目指す「物理実機による実時間実証」はこのエミュレーション段階を実機へ引き上げるものと位置づけられる。

**2. 関連研究領域の全体像**
- 連合学習の基礎：McMahan et al. (2017) のFedAvgが起点。原論文「Communication-Efficient Learning of Deep Networks from Decentralized Data」（arXiv:1602.05629 / AISTATS 2017）はverbatimで「we show a reduction in required communication rounds by 10-100x as compared to synchronized stochastic gradient descent」と述べる。FedProx（Li et al. 2018）等がNon-IID対応を進めた。
- 完全分散/P2P学習：Gossip学習（GoSGD, Blot et al. 2016）、D-PSGD（Lian et al. 2017）、そしてKoloskova et al. (2020) 「A Unified Theory of Decentralized SGD with Changing Topology and Local Updates」（ICML）が時変トポロジー下の収束を統一的に理論化。本プロジェクトの時変トポロジーはこの理論系譜に接続する。
- Federated LoRA：FedIT（LoRA×FedAvgの初期統合）、FLoRA（Wang et al. 2024, NeurIPS、異種ランクLoRAのstacking集約）、FDLoRA（dual LoRA）、FedEx-LoRA（正確な集約）、LoRA-FAIR（ICCV 2025）など。多くは中央サーバー前提で、完全分散P2P×LoRAは相対的に手薄。
- 計算・通信オーバーラップ：ZenFlow（stall-free offloading）、Megatron/NeMoのcomm_overlap、パイプライン並列（GPipe, PipeDream）など。これらは主にデータセンター内同期学習向けで、P2P機会的接触下でのストールフリーは新規領域。
- モバイル/エッジLLMファインチューニング：PocketLLM、MobiLLM、MobileFineTuner等が実機のメモリ制約下でのファインチューニング可能性を示すが、いずれも協調学習の時変トポロジー実機実証は扱っていない。
- 応用の受け皿：DTN（Delay Tolerant Network）/機会的ネットワークの災害・僻地応用の系譜が確立しており、WAFL-PEFTの応用先として直結する。

**3. 実装・評価・新規性・応用の要点**
- 実装：計測レイヤ（ストール時間、GPU使用率、スループット時系列）、決定論的再現性（BlazeFL的RNG分離）、通信効率化（Top-K差分スパース化：同研究室Tsuchiya et al. CAI 2025）、耐障害性（ノード離脱・毒性モデル耐性：Tezuka et al. 2022）の統合が必要。
- 評価：スループット平坦性（変動係数・ストール率）、時変トポロジー下の知識収束（GSM8K精度 vs self-training/理想FL）、壁時計時間あたりの収束、スケール依存性（10→70台）。
- 新規性：実機大規模P2P×LLM-PEFT×リアルタイム完全非同期オーバーラップの三点同時実証。
- 応用：医療・金融のプライバシー保護協調、通信インフラ不安定地域、IoT/エッジ、災害時ネットワーク。

## Details

### セクション1：先行研究サーベイ

**1-1. WAFLの源流と発展（東京大学 落合・江崎研）**
WAFLは、プライバシー機微データを保持する自律移動ノード（車両・スマートデバイス・センサ）が、第三者サーバーに依存せず近接ノード間のデバイス間（D2D）通信のみで協調学習する枠組みとして2022年に提案された（Ochiai et al., arXiv:2205.11779）。各ノードは局所Non-IIDデータで個別訓練し、接触時にモデルパラメータを交換・集約して「弱同期（weak synchronization）」を行うことで、局所特化モデルを汎化モデルへと収束させる。評価では静的4トポロジー（line/tree/ringstar/fullmesh）と動的2種（Random Waypoint、Community-Structured Environment）のcontact patternを用い、動的ケースでは接触係数λを小さく（λ=0.1）した方が良好という知見が示された。self-training（84.7%）に対しWAFLは94.8〜96.3%（MNIST 90% Non-IID、10ノード、2層全結合NN）。

WAFLはその後多方面へ拡張された：WAFL-GAN（Tomiyama et al. 2023）、WAFL-ViT（Ochiai et al. 2023, Best Paper）、WAFL-DETR/YOLO（物体検出）、WAFL-Whisper（音声認識）、毒性モデル耐性の理論解析（Tezuka et al., TPS-ISA 2022）、疎動的網での効率集約MemWAFL（Eguchi et al. 2023）、階層トポロジー自己組織化（Ito et al. 2023）、ラベル選好スキュー対応の個別化WAFL（Higuchi et al. 2023）、通信効率化のTop-K差分スパース化＋量子化（Tsuchiya et al., IEEE CAI 2025）、VQAの完全分散協調学習（Ueda & Ochiai, CAI 2025）など。コードはgithub.com/jo2lxq/waflで公開（GPL-3.0）。

**1-2. 「中島氏ら(2026)」に相当する研究**
仕様書が参照する「中島氏ら(2026)の離散ラウンド制シミュレーションデータ」は、同研究室の中島幸太（Kota Nakajima）氏らによる2026年の会議論文「Serverless Cross-Device Knowledge Transfer with Collaborative LLM Fine-Tuning via Wireless Ad Hoc Federated Learning」に対応すると考えられる。同じく田中幹起（Motoki Tanaka）氏らの「Robust Training of Large Language Models under Non-IID Data in Wireless Ad Hoc Federated Learning」（2026年）、Yang Guangzhao氏らの「Singular Value Fine-Tuning for Efficient Device-to-Device Adaptation of Large Language Models」（2026年）も同系統のWAFL×LLM研究群である。これらはWAFLをLLM協調ファインチューニングへ拡張した最新研究で、いずれもWAFL標準のエポック/ラウンド制・contact_pattern駆動シミュレーションを踏襲していると見られる。なお「中島」を含む別人物「Jin Nakazato（中里仁）」はmmWave/V2X/O-RAN分野の研究者でWAFLシミュレーション研究は確認されなかったため、該当は中島幸太氏である可能性が高い。

これら先行研究の限界は、(a)シミュレーション/エミュレーションベースで実時間の計算・通信干渉やGPUアイドルを観測できない、(b)離散ラウンド制のため計算と通信が逐次的（ストールが構造的に発生）、(c)ノード数が実機制約で小規模、という点にある。本プロジェクトはまさにこの3点を実機・実時間・大規模で克服する設計になっている。

**1-3. 連合学習・分散最適化の基礎**
FedAvg（McMahan et al. 2017「Communication-Efficient Learning of Deep Networks from Decentralized Data」, arXiv:1602.05629 / AISTATS 2017）が連合学習の基礎で、局所計算を増やし同期SGD比で通信ラウンドを10〜100倍削減（原論文verbatim: "a reduction in required communication rounds by 10-100x as compared to synchronized stochastic gradient descent"）。Non-IIDはFLの本質的課題で、FedProx（Li et al. 2018）等が対処。完全分散側ではGossip学習（GoSGD, Blot et al. 2016）、D-PSGD（Lian et al. 2017、分散SGDが集中型と同等の線形加速を達成しうることを示した）、非同期版（DRACO, arXiv:2406.13533）などがある。時変トポロジーの理論的支柱はKoloskova et al. (2020, ICML)「A Unified Theory of Decentralized SGD with Changing Topology and Local Updates」で、適応的ネットワークトポロジー下の局所更新・gossip更新の統一収束率を導出し、データ不均一性が収束を阻害しうることを示した。本プロジェクトの時変トポロジー実機実証はこの理論の実証的検証にもなりうる。

**1-4. Federated LoRA / PEFT**
LoRA（Hu et al. 2021）は低ランク行列で微調整パラメータを圧縮するPEFTの代表。連合学習との統合ではFedIT（LoRA×FedAvg初期統合）、FLoRA（Wang et al. 2024, NeurIPS、異種ランクLoRAのノイズフリーstacking集約）、FDLoRA（dual LoRAで個別知識と大域知識を分離）、FedEx-LoRA（正確集約）、LoRA-FAIR（ICCV 2025、集約・初期化のバイアス低減）、DP-FedLoRA（差分プライバシー）などが活発。医療Non-IIDへの適用（Flow of Knowledge, arXiv:2510.00543）も登場。GSM8Kを用いた連合LoRA評価としてはFedBiOT（LLaMA-2, GSM-8K）、SFed-LoRA、そして「Wireless Federated Multi-Task LLM Fine-Tuning via Sparse-and-Orthogonal LoRA」（arXiv:2602.20492、Qwen2.5-1.5B/7B, GSM8K含む、分散接続）などがある。ただしこれらの大半は中央サーバー前提かデバイス数15台以下・シミュレーションで、完全分散P2P×実機大規模×時変トポロジーは空白。

**1-5. 計算・通信オーバーラップ（システム研究）**
ストールフリー/オーバーラップの代表はZenFlow（stall-free offloading engine、CPU更新・転送をGPU計算と重ねる）、Megatron/NeMoのcomm_overlap（DP/TP/PP通信を計算と重畳）、GPipe/PipeDream（パイプライン並列でforward/backwardを重畳）、EmbRace（Computation Stall指標で通信効率を評価、16×RTX3090で1.45〜2.56倍高速化）など。特筆すべきはGossip SGDにおける計算・通信オーバーラップ研究（「An Asynchronous Distributed Training Algorithm Based on Gossip」）で、非同期gossipではダブルバッファリングが使えない課題に対し通信スケジューリングでオーバーラップを実現した。これらは同期データセンター向けが主流で、P2P機会的接触・時変トポロジー下のリアルタイム完全オーバーラップは本プロジェクトが切り込む新領域。

**1-6. モバイル/エッジLLMと機会的ネットワーク応用**
PocketLLM（MeZOでスマホ上RoBERTa-large微調整を約4GBで実現）、MobiLLM（サーバー支援side-tuning）、MobileFineTuner（Android上のPEFT/Full-FT）等が実機LLM微調整の実現可能性を示す。応用基盤としてはDTN/機会的ネットワークの災害・僻地通信研究（store-carry-forward、epidemic routing、災害避難誘導）が確立しており、時変トポロジーWAFL-PEFTの直接の応用シナリオとなる。

### セクション2：実装すべき機能

**2-1. 計測・可観測性レイヤ（最優先）**
ストールフリーの主張には定量的裏付けが不可欠。実装すべきは：(a)スレッド別タイムライン計測（訓練ループ／P2P TCP交換・マージ／ディスク書出しの各スレッドの稼働・待機時間）、(b)GPUアイドル率・SM利用率の時系列（nvidia-smi/NVML/DCGM）、(c)スループット時系列（tokens/s、samples/s、iterations/s）とその変動係数、(d)ストール時間の定義と計測（EmbRaceの「Computation Stall」指標に倣い、通信で覆い隠せなかった計算停止時間）、(e)接触イベントログとcontact_pattern.jsonの実時刻整合性検証。

**2-2. 実験再現性・決定論性**
物理実機は非決定的要因（スケジューリング、ネットワークジッタ）が多いため、BlazeFL（arXiv:2604.03606、スレッド並列でのRNG分離による決定論的再現）に倣い、シード管理・contact_patternの実時刻同期・NTP的時刻合わせを実装。実機ゆえの非決定性は「観測対象」として記録し、シミュレーションとの差分を明示する設計に。

**2-3. 通信効率化とスケーラビリティ**
70台規模のLoRAパラメータ交換に備え、同研究室のTop-K差分スパース化＋量子化（Tsuchiya et al. 2025）やMemWAFL的な疎動的網向け効率集約を組み込む。生TCP交換層は差分のみ送る設計、非同期ディスク書出しはダブルバッファリング。マージ層はLoRAのA・B行列の集約（FLoRAのstacking、あるいは加重平均λ制御）を選択可能に。

**2-4. 耐障害性・セキュリティ**
実機ではノード離脱・再参加が現実に起こる。毒性モデル耐性（Tezuka et al. 2022の理論）、ビザンチン耐性集約、接触相手認証を評価オプションに。オープンソース化を見据えdockerイメージ・mise task・contact_pattern生成器（RWP/CSE/実測トレース）を同梱。

**2-5. 差別化要素**
先行のエミュレーション基盤（Shah et al. KST 2026）に対し、本プラットフォームは「物理実機・実時間・4スレッド完全非同期・50〜70台・LLM-PEFT」を統合する点で差別化。特にcontact_pattern.jsonで経過秒数ごとにトポロジーを定義し実時間で駆動する点、サーバー交信リスナーを持ちつつ学習は完全分散という「ハイブリッド計装」は評価基盤として独自性が高い。

### セクション3：期待される結果

**3-1. スループット平坦性（ストールフリー）**
4スレッド構成（サーバー交信リスナー／P2P生TCP交換・マージ／LoRA訓練ループ／非同期ディスク書出し）により、通信・マージ・I/Oが訓練計算とオーバーラップし、GPUアイドルが最小化される想定。期待結果としては、逐次実行（ストールあり）ベースラインに対しストール率を大幅に低減し、スループット時系列の変動係数が小さく「平坦」であることを示す。接触イベント発生時にもスループットが落ち込まない（スパイク状の停止が生じない）ことがストールフリーの核心的証拠となる。定量目標はGPUアイドル率・ストール率を実測ベースライン（逐次実行版）との相対比較で提示すべき（絶対値は実機構成依存のため、比率で示すのが頑健）。

**3-2. 時変トポロジー下の知識収束**
GSM8K数学推論で、self-training（孤立訓練）に対しWAFL-PEFTが明確な精度向上を示し、理想的な集中FL（またはfullmesh静的）に漸近することが期待される。WAFL原論文がMNISTで示した「self-training発散 vs WAFL収束」の構図を、LLM-PEFT×GSM8Kという知識収束タスクで再現・拡張する。時変トポロジーでは静的より収束が遅い（接触待ちで訓練スキップが発生）が、最終精度は接近するという結果が既存WAFL知見と整合する見込み。既存研究との比較で示すべきは、(a)離散ラウンド制シミュレーションと実時間実機の収束曲線の差、(b)接触係数λの最適値が動的トポロジーで小さくなる現象のLLMでの再現。

**3-3. 実機大規模（50〜70台）の意義**
シミュレーションでは捨象される要素——実ネットワークのジッタ・パケット再送、スレッド競合、GPUメモリ帯域競合、ディスクI/O遅延、時計ずれ——が収束とスループットに与える影響を初めて定量化できる。50〜70台という規模は、gossip/分散SGD研究で「大規模（100台超）ではnaive gossipが通信不均衡・収束劣化を起こす」（GossipGraD）とされる領域の入口にあたり、スケール依存性（10→70台での収束速度・スループットの変化）を実機で観測できる点に固有の学術的価値がある。参考として、シミュレータPeerFL（Luqman et al., arXiv:2405.17839）はNS3上で「a maximum of 450 heterogeneous devices modelled as participants」と最大450台を模擬しているが、これはあくまでシミュレーションであり、本プロジェクトの物理実機50〜70台とは質的に異なる。

### セクション4：新規性

1. **実機大規模P2P分散学習基盤としての新規性**：既存WAFL研究の大半は10ノード級のシミュレーション。PeerFL（NS3上で最大450台シミュレーション）などのシミュレータは存在するが、物理実機50〜70台（ThinkPad）＋GPU 10台での協調LLM-PEFTは前例が乏しい。Shahら（KST 2026）のエミュレーション基盤を実機へ引き上げる正統な発展。
2. **計算・通信の完全非同期オーバーラップのリアルタイム実証**：離散ラウンド制（計算→通信の逐次）を前提とする既存WAFL/連合LoRAに対し、4スレッドで計算・通信・I/Oを実時間で重畳しストールフリーを実証する点。データセンター向けオーバーラップ技術（ZenFlow等）をP2P機会的接触・時変トポロジーに持ち込む点が新しい。
3. **WAFL×LLM-PEFT×知識収束タスクの実機統合**：中島幸太氏ら2026年のWAFL×LLM研究、田中氏らのNon-IID下LLM堅牢訓練を、GSM8K数学推論という「知識収束が可視化しやすい」タスクで、実機・実時間・大規模に統合。
4. **時変トポロジーの実時間駆動**：contact_pattern.jsonで経過秒数ごとにP2Pトポロジーを定義し実時間で駆動する計装は、Koloskova et al. (2020)の時変トポロジー理論の実証実験基盤として独自。

### セクション5：社会貢献・応用可能性

- **プライバシー保護が必須の医療・金融**：生データを一切交換せずLoRA差分のみ共有するWAFL-PEFTは、病院間・金融機関間の協調LLM微調整に直結（Flow of Knowledge等の医療Non-IID連合LoRAの実機分散版）。中央サーバー不要ゆえマルチベンダー・規制対応で有利。
- **通信インフラ不安定地域・僻地**：中央集約に依存しないP2P協調は、常時接続が得られない環境でのAI高度化に適合。DTN/store-carry-forwardの系譜と結合可能。
- **IoT/エッジコンピューティング**：スマホ・車載・センサ群がすれ違い接触でモデルを育てるユースケース（WAFL originの想定シナリオ）をLLM時代に拡張。
- **災害時ネットワーク**：インフラ途絶下で端末同士の機会的接触のみでAIを協調学習・維持（災害避難誘導の機会的通信研究と親和）。時変トポロジーは災害時の断続接続を忠実にモデル化。
- **オープンソース貢献**：Docker・miseタスク・contact_pattern生成器・計測レイヤを含む再現可能な実機ベンチマークとして公開すれば、WAFLコミュニティ（github.com/jo2lxq/wafl）やP2P連合学習研究（PeerFL, BlazeFL等）に対する「実機大規模・実時間」の標準評価基盤を提供できる。

## Recommendations

**段階1（基盤確立、初期）**：計測・可観測性レイヤを最優先で実装。GPUアイドル率・スレッド別稼働時間・スループット時系列・ストール時間を記録。まず10台規模で「逐次実行版 vs 4スレッド版」のストール率比較を取り、ストールフリーの定量的定義を確定する。ベンチマーク閾値：4スレッド版のGPUアイドル率が逐次版比で有意に低下し、スループット変動係数が小さいこと。

**段階2（知識収束の実証）**：GSM8K Non-IID分割でself-training・時変WAFL-PEFT・理想FL（fullmesh）の収束曲線を比較。接触係数λをスイープし動的トポロジーでの最適λを同定。閾値：時変WAFL-PEFTがself-training比で明確な精度向上を示し、理想FLに漸近。示せない場合はマージ則（LoRA A/B集約法、λ）を見直す。

**段階3（スケール実証）**：10→50→70台へ拡大し、収束速度・スループット・ストール率のスケール依存性を測定。100台超で報告される通信不均衡・収束劣化の兆候を観測。閾値：70台でもスループット平坦性が維持されること。維持できなければTop-K差分スパース化・量子化を導入。

**段階4（頑健性・公開）**：ノード離脱/再参加・毒性モデル耐性を評価に追加し、Docker/mise/contact_pattern生成器・計測ログ仕様を整備してオープンソース公開。先行のShah et al. (KST 2026)エミュレーション基盤、中島幸太氏らWAFL×LLM研究を明示的に引用し、本研究を「実機・実時間・大規模」への拡張として位置づける。

**論文化の戦略**：主張の軸を「シミュレーション/エミュレーションでは観測不能な実機由来の現象（ストール、ジッタ、スケール劣化）の定量化」に置く。投稿先はMobiCom/MobiHoc/INFOCOM（システム・ネットワーク面）またはIEEE CAI/WF-IoT（WAFL系譜の実績venue）が有力。

## Caveats
- Shah et al.（KST 2026, DOI 10.1109/KST67832.2026.11431966）の内部アーキテクチャ・データセット・定量結果はIEEE Xplore有料で本文未確認。書誌情報は確定だが、内容は直接アクセスで検証が必要。
- 「中島氏ら(2026)」を中島幸太（Kota Nakajima）氏の2026年WAFL×LLM研究と同定したのは、研究室著者ページ・dblp・タスク記述の整合からの推定。正確な会議名・DOI・使用データセット（LoRA/GSM8K採用の有無）・定量結果は原論文で確認すべき。別人物「Jin Nakazato」はV2X/mmWave研究者でWAFLシミュレーション研究は確認されず、該当しない可能性が高い。
- WAFL原論文の精度はアブストラクトでは94.8〜96.3%、本文表記では94.7〜96.2%と若干の差異があり、引用時は出典版を明記すべき。
- 期待される定量結果（ストール率・精度向上幅・スケール依存性）は既存WAFL/オーバーラップ研究からの外挿であり、実機実験で検証されるべき仮説。特に絶対値は実機構成に強く依存するため、逐次実行版・self-training・理想FLとの相対比較で報告するのが頑健。
- 一部の2026年論文（arXiv:2602.20492等）は極めて新しく、査読・最終版が未確定の可能性がある。