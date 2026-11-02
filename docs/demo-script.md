# デモの台本と試験の対応

3分の動画の場面ごとに、画面で何を操作し、どの試験がその操作を通しているか、README のどの数字を使うかをまとめました。せりふと字幕は台本（v3）の物で、ここには書き写していません。場面は台本の時刻で示します。

流れは、表を作る人の一言で表を直す（1幕）→ 待機当番を言葉で足し、休みの電話で待機の人だけが動く（2幕）→ 12月の返す案と返さない案（3幕）です。

| 台本の場面 | 画面の操作 | それを通す試験 | その場面で使う README の数字 |
|---|---|---|---|
| 0秒〜20秒 つかみ（11月の表） | `uv run shiftvoice demo` で開いた11月の表を見せる（操作なし） | `tests/test_solver.py::test_s1_november_keeps_all_rules` | 11月の表は動かせない決まりの外れ0件、初めての起動から約10秒 |
| 20秒〜40秒 遅番の次の日に早番 | 一言を言い、「毎月ですか、今月だけですか」に［毎月］。当てはまるマスが光って直る | `tests/test_t1_act1.py::test_t1_act1`、`tests/test_utterance.py::test_r10_l3_late_early_asks_then_fixes` | 当てはまり11件を、21マスを動かして直す（1.4秒） |
| 40秒〜55秒 試験までの夜勤 | 一言を言う。ルールに理由と「29日まで」が残る | `tests/test_utterance.py::test_l1_l7_kojima_until_29` | 一言で直す1幕の8件は、どれも0.4秒以内 |
| 55秒〜1分10秒 待機当番を言葉で足す | 一言を言い、Claude が組み立てた当番のルールを読み返す | `tests/test_act2.py::test_d1_standby_by_words` | 本物の Claude Sonnet 5.5 で待機当番を組み立てる、10回すべて同じ定義、1回2.2秒前後 |
| 1分10秒〜1分25秒 採用と試算 | ［採用］で待機の欄ができ、土日に担当者が入る。待機当番のルールに試算の2行が出る | `tests/test_standby.py::test_d2_assign_weekends`、`tests/test_absence_sim.py::test_absence_sim_reproduces`、`tests/test_absence_sim.py::test_absence_sim_in_state` | 待機が無い時、土日の急な休み76件のうち46件で休みの人に電話が要り、小島さん9回・田村さん6回（14人の平均は約3回） |
| 1分25秒〜1分50秒 休みの電話 | 配った後に休みの電話の一言を言う。待機の小島さんに頼む案（2マス）だけが光り、［了承をもらって直す］ | `tests/test_act2.py::test_d3_d5_accept`、`tests/test_standby.py::test_d3_only_duty_moves` | 本物の Claude Haiku 5.5 で撮影と試験の一言36件を読む、3周108回すべて用意と同じ |
| 1分50秒〜2分02秒 12月のルールの見直し | 期限が来た試験のルールを［履歴へ片付ける］、ほかの2件は［今月も使う］ | `tests/test_act3.py::test_act3_flow`、`tests/test_review.py::test_m10_expired_never_applies` | （数字の行は使わない） |
| 2分02秒〜2分30秒 山場（小島さんの12月） | ［12月の表を作る］。小島さんの主表示の1行を見せる | `tests/test_act3.py::test_act3_flow`、`tests/test_act3.py::test_hard_rules_zero_nov_dec` | 引き受けた3人とも有給 0→1日、いちばん長い連勤 5→4日（1人は4→3日） |
| 2分30秒〜2分45秒 返す案の表 | 返さない案から変わったマスだけが光る。［12月はこれで作る］ | `tests/test_giveback.py::test_b1_both_plans_same_conditions`、`tests/test_act3.py::test_act3_flow` | 返さない案から変わるのは5マス。ほかの人の5日続く勤務の窓 16→16・飛び石 0→0、9秒 |
| 2分45秒〜3分00秒 締め | 引き受けの記録に戻る（操作なし） | `uv run pytest -q` の全件 | 台本の3幕を本物の Claude で通す、8回とも13件が通る |

## 気をつけること

- 3幕（1分50秒〜）の画面は、撮影の11月末の状態から作ります。作り方は引き継ぎの「撮影の状態の作り方」にあり、試験では `tests/test_act2.py` の `kojima_rest` が同じ操作（14日と22日の休みの電話を小島さんが了承）を行います
- 台本の締めのせりふにある試験の件数は、台本を書いた時点の数です。試験は増えているので、撮る日に `uv run pytest -q` の件数を数え直します
- 台本の3幕を本物の Claude で通す時は、鍵を入れて `SHIFTVOICE_LIVE=1 uv run pytest -q tests/test_t1_act1.py tests/test_act2.py tests/test_act3.py` を流します
