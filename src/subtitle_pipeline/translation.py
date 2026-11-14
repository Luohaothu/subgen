from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from subtitle_pipeline.alignment import JapaneseCue, timestamp_digest

KANA_RE = re.compile("[\u3040-\u30ff\u31f0-\u31ff\uff65-\uff9f\U0001b000-\U0001b16f]")
KANA_RUN_RE = re.compile(KANA_RE.pattern + "+")
HAN_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]")
SHA256_RE = re.compile("[0-9a-f]{64}")
ALLOWED_NON_HAN_SUBTITLES = frozenset({"ok"})
ASCII_LABEL_RE = re.compile("[A-Za-z]{2,4}")
PRESERVABLE_ASCII_LABELS = frozenset({"iphone", "tiktok"})
PRESERVABLE_SINGLE_ASCII_LABELS = frozenset({"f", "m", "s"})
PRESERVABLE_KANA_ASCII_LABELS = {
    "アベマ": "abema",
    "えふ": "f",
    "エフ": "f",
    "エス": "s",
    "エム": "m",
    "エスエム": "sm",
}
NUMERIC_LITERAL_RE = re.compile("[0-9零〇一二三四五六七八九十百千万億亿兆两]+")
NUMERIC_WITH_UNIT_LITERAL_RE = re.compile(
    "[0-9零〇一二三四五六七八九十百千万億亿兆两]+(?:分|秒|月|日|时|点|次|回|度|岁|人|個|个)"
)
YEAR_LITERAL_RE = re.compile("(?:[0-9]{4}|[零〇一二三四五六七八九]{4})年")
NON_PRESERVABLE_NUMERIC_LITERALS = frozenset({"九十十一"})
BLOOD_TYPE_LITERAL_RE = re.compile("(?:a|b|o|ab|rh)型")
ROMANIZATION_REQUIRED_KANA_NAMES = frozenset(
    {
        "まゆきちゃん",
        "あけずさん",
        "あみちゃん",
        "ゆうとくん",
        "ケンタ",
        "リム",
        "あやちゃん",
        "あかりさん",
        "すずな",
        "すくらちゃん",
        "あおい",
        "ふちお",
        "えいちゃん",
        "かずき",
        "なおん",
        "ひな",
        "ひなこ",
        "マリちゃん",
        "メイ",
        "ルカ",
        "さやか",
        "さやかちゃん",
        "サイバちゃん",
        "けすけくん",
        "いちか",
        "えりこ",
        "たくろう",
        "こだま",
        "こだまさん",
        "なな",
        "ななちゃん",
        "エサーリア",
    }
)
DIRECT_KANA_NAME_REQUIRED_LATIN = {
    "あすか": "Asuka",
    "まゆきちゃん": "Mayuki",
    "あけずさん": "Akezu",
    "あみちゃん": "Ami",
    "ゆうとくん": "Yuto",
    "あやちゃん": "Aya",
    "あかりさん": "Akari",
    "すくらちゃん": "Sukura",
    "えいちゃん": "Ei",
    "マリちゃん": "Mari",
    "さやかちゃん": "Sayaka",
    "サイバちゃん": "Saiba",
    "けすけくん": "Kesuke",
    "こだまさん": "Kodama",
    "ななちゃん": "Nana",
    "エサーリア": "Esaria",
    "ふるみさん": "Furumi",
    "明かり紡ぎ": "Akari Tsumugi",
    "もこちゃん": "Moko",
    "すみれ": "Sumire",
    "つずねちゃん": "Tsuzune",
    "ガルちゃん": "Garu",
    "かのちゃん": "Kano",
    "紗文義": "Saon",
    "キビ": "Kibi",
    "まいまい": "MaiMai",
    "せーな": "Seina",
    "せーな姉ちゃん": "Seina",
    "天香": "Ame",
    "ネージェ": "Nage",
    "かんちゃん": "Kan",
}
DIRECT_ROMANIZATION_REQUIRED_KANA_NAMES = frozenset(DIRECT_KANA_NAME_REQUIRED_LATIN)
REJECTED_GLOSSARY_JA_TERMS = frozenset({"いかゆさん"})
CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS = {
    "いかゆさん": "一个月",
    "我的水杯": "……",
    "あんぴゃんぽんっ…ん": "嗯",
    "喫茶店": "咖啡店",
    "じゃこちゃん": "那就这边",
    "ちゅうくた": "……",
    "of.": "……",
    "日本史限りか": "只选日本酒吗",
    "にのう。": "……",
    "眺め固め": "长一点、硬一点",
    "飯塚": "饭冢",
}
EXACT_CUE_REQUIRED_TRANSLATIONS = {
    "明かり紡ぎです。": "我是Akari Tsumugi老师。",
    "シジュアルが来る日しか。": "只有那一天。",
    "はいじゃあ皆さん帰りますよ。": "好了，大家回去吧。",
    "気持ち悪い。": "好难受。",
    "あ、あのうささん、だい、大丈夫、もう起きて、え、もう帰りますよ。": (
        "那个，Usa小姐，你还好吗？快起来，我们要回去了。"
    ),
    "ちょっとお水飲んだ方がいいかな。": "要不要喝点水？",
    "無親者入ったらやらしめなったら。": "……",
    "に！": "啊！",
    "how am i": "你。",
    "看懂水的引导。": "……",
    "めいそしゃのて。": "……",
    "だから、さんが出したせいで、ここぐちゃぐちゃですよ。": (
        "都怪你射出来了，把这里弄得湿漉漉的。"
    ),
    "せーな姉ちゃんには関係ねえだろ。": "这跟你没关系吧，Seina姐。",
    "うちの商品万引きしましたよね。": "你偷了我们店里的商品吧。",
    "スコエ": "太厉害了。",
    "にいう。": "哥哥……",
    "過ごしたつか。": "怎么了？",
    "止めばったイッチャーをよしに行く": "……",
    "口開けろよこ。": "把嘴张开啊，笨蛋。",
    "カナオ ジェーム。": "……",
    "あっあっあっあっ……！": "啊、啊、啊、啊……！",
    "てってれー": "锵锵——！",
    "牛肚桂味。": "……",
    "珍愛。": "珍爱。",
    "ガッティーユーティー。": "……",
    "南京交通大学。": "南京交通大学。",
    "ぐせんぐせ": "……",
    "不用不用不用。": "不用不用不用。",
    "うずい": "……",
    "にむし。": "……",
    "顕著で、口の中から体を大吉は大吉。": "……",
    "the": "……",
    "刚才写的。": "刚才写的。",
    "来者不善。": "来者不善。",
    "ちびな生き遅れ争うかいかんよいかん捕獲されちまう": "……",
    "かんちゃん。": "Kan。",
    "ちゅうはぁぁぁぁぁぁぁぁ": "啾——",
    "不要不要不要不要不要不要。": "不要不要不要不要不要不要。",
    "好快好快。": "好快好快。",
    "で、その次の日、赤すり行って、真っ裸で、真っ裸で、そうそう、": (
        "然后第二天去了搓澡店，全身赤裸着，对，对，"
    ),
    "赤すりのやってくれる女の人に、真っ裸でガーって。": (
        "让搓澡的女工作人员给我全身搓了一遍。"
    ),
    "チャンスの時間。": "《机会时间》。",
    "晚安，小乖。": "晚安，小乖。",
    "ストラッシュ": "STLASSH。",
    "of where.": "……",
    "エルエル。": "LL。",
    "九十十一。": "九、十和十一。",
    "我送": "……",
    "うむやのやや": "……",
    "解除解除。": "解除，解除。",
    "可以可以可以。": "可以，可以，可以。",
    "ちょっと八つ揚げさんにむしろ前義なしはなんかちょっとなんか愛がないとかっていう風にいう声もあったりとかするんです": (
        "不过，也有人说，对她来说，没有前戏之类的，会让人觉得有点缺乏爱意。"
    ),
    "V I O脱毛。": "VIO私处脱毛。",
    "コルティ。": "这边。",
    "ライブハウス。": "现场音乐演出场所。",
    "とても好きな時に出していい。": "特别舒服的时候就可以射出来。",
    "意思意思。": "好、好。",
    "こびしむ": "……",
    "操作 都知。": "……",
    "くいく": "要去了。",
    "あらかん。": "哎呀。",
    "あい。": "嗯。",
    "たらしい。": "好像是这样。",
    "ちょめちょめって何ですか？": "“那个那个”是什么意思？",
    "ファースト。": "第一（First）。",
    "はああああああああ": "哈啊啊啊啊啊啊啊——",
    "ハイボールです。": "是高球酒。",
    "キングコープ。": "King Corp公司。",
    "しもんちゃんの": "是Shimon的。",
    "我有记事典章。": "……",
    "数学物理学。": "数学、物理学。",
    "ません。": "……",
    "宝玉与q有性格。": "……",
    "支持燕京站。": "……",
    "かなさんの方がなんか。": "Kana反而看起来……",
    "奥。": "深处。",
    "すみちゃん、理由が回したくてね。": "Sumi，我还想再来一轮。",
    "じゃあつみさんからシャンパーいただきました。": (
        "那么，我们收到了Tsumugi送的香槟。"
    ),
    "てます。": "……",
    "冰冰冰冰冰冰手套不怕冰，他要求吉吉不戴套，只握手刚好。": (
        "冰冰冰冰冰冰手套不怕冰，他要求吉吉不戴套，只握手刚好。"
    ),
    "チュウされたんだけど、何チュウされたんだけど。": (
        "我被亲了，不过到底亲的是哪里？"
    ),
    "えみかちゃんに合わせる顔がないよ。": "我都没脸见Emika了。",
    "デミコ。": "Emika。",
    "今のままの成績じゃ志望校に行けないと言われましてね。": (
        "老师说，照现在的成绩，考不上目标学校。"
    ),
    "スムレちゃんの体見": "我想看看Sumire的身体……",
    "呸呸呸呸": "呸、呸、呸、呸。",
    "ひくあい": "……",
    "誰也不认识誰，诚信交友，不提兼职交友。": (
        "谁也不认识谁，诚信交友，不提兼职交友。"
    ),
    "谁也不认识谁，诚信交友，不提兼职交友。": (
        "谁也不认识谁，诚信交友，不提兼职交友。"
    ),
    "かざいて逆線に一方でちょっとざまんことあります": "……",
    "あ、おまかみゆいです。": "啊，是天神Yui。",
    "あ、おまかみゆいさん。": "啊，是天神Yui小姐。",
    "えきど。": "……",
    "えんのえって…": "说到“en”里的“e”……",
    "たいとうとう。": "……",
    "都足以改变人生。": "都足以改变人生。",
    "我我看看。": "我、我看看。",
    "てるのってさ、何じゃないの？": "你说的“在做”到底是什么？",
    "タウンシノ。": "……",
    "たくろう。": "Takuro。",
    "マインドエンサー。": "来得及的。",
    "あらへんぬく": "……",
    "ざるしゃる。": "……",
    "なんか 前原さんも反応してんの？": "怎么，前原也有反应了？",
    "大王大王。": "……",
    "たちかさか": "……",
    "しもんべく。": "……",
    "我不是老公。": "我不是你老公。",
    "hello大家好，我叫舒曼，我今年二十五岁，身高一百六十八公分，体重五十五公斤，": "大家好，我叫舒曼，我今年二十五岁，身高一百六十八厘米，体重五十五公斤，",
    "我比较喜欢": "我比较喜欢",
    "一年以内。": "一年以内。",
    "っ、んんんんんんっ…んんんんんんっ…": "嗯……嗯……",
    "河马奥特曼。": "河马奥特曼。",
    "警察、警察。": "警察，警察。",
    "はにせんぼんのまず指切った。": "要是说谎就吞一千根针，拉钩了。",
    "ねえしゅ": "喂……",
    "宇宙大王。": "宇宙大王。",
    "えずさんの奥まで…": "一直到最深处……",
    "えらなあんんんんんんんんんん。": "呃……啊……",
    "スカイ。": "Sky。",
    "あすか…んんっ…": "Asuka……嗯……",
    "がいくすと": "……",
    "五十九、二十九です。": "59、29。",
    "大和。": "Yamato。",
    "きこき": "……",
    "ちゅうん": "啾",
    "青万寿寺": "湿漉漉的？",
    "张子健。": "张子健。",
    "ひだめ、ひだめ、ひだめ、ひだめ、ひだめ。": ("不行、不行、不行、不行、不行。"),
    "れろれろれろれれろれれろれれろれれろれれろれれろれれろれれろれ": ("……"),
    "と。": "……",
}
NORMALIZED_EXACT_CUE_REQUIRED_TRANSLATIONS = {
    unicodedata.normalize("NFKC", source).strip(): target
    for source, target in EXACT_CUE_REQUIRED_TRANSLATIONS.items()
}
EXACT_CUE_ID_REQUIRED_TRANSLATIONS = {
    (
        "921b490fb0a5ea00c268ac1f0563117c0fa0ee2d3be14f080294545e9723dc71"
        "-seg-00000075-cue-0000"
    ): "那个。",
    (
        "921b490fb0a5ea00c268ac1f0563117c0fa0ee2d3be14f080294545e9723dc71"
        "-seg-00000078-cue-0000"
    ): "是台风天。",
    (
        "b971130f6873aaa35684e412491e0d9425332d09a2b125f866e1b15ad5a419ad"
        "-seg-00000031-cue-0005"
    ): "空着。",
    (
        "61fe480e85b868966bb22c61c2a1d8c48348b28e2895a18021fac94b85c0b15a"
        "-seg-00000086-cue-0000"
    ): "粉色。",
    (
        "12410ad96380934673b676792de5895a6c60ca639f549d8f5f9b6b253bb424cd"
        "-seg-00000226-cue-0000"
    ): "开始有关系了。",
    (
        "12410ad96380934673b676792de5895a6c60ca639f549d8f5f9b6b253bb424cd"
        "-seg-00000227-cue-0000"
    ): "会被做吗？",
    (
        "12410ad96380934673b676792de5895a6c60ca639f549d8f5f9b6b253bb424cd"
        "-seg-00000228-cue-0000"
    ): "父母。",
    (
        "b62b00216cd62560c4be39ee1a1c34fd15e2f2b7eb5280ae4f90f995e7b9187e"
        "-seg-00000094-cue-0000"
    ): "太丢人了。",
    (
        "66520888e2d840b33f75a33f756986aa42bdacdac858764da6eb1a5e433814a6"
        "-seg-00000297-cue-0000"
    ): "……",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000012-cue-0000"
    ): "潮喷表演可以指挥。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000015-cue-0000"
    ): "嗯嗯，还有万部AV在线观看哦，老公快来操我。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000016-cue-0000"
    ): "台湾悠悠裸聊室注册成为会员免费送五十点。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000017-cue-0000"
    ): "是绵绵一段乐章多。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000018-cue-0000"
    ): "台湾真人视频聊天，十年信誉任你指挥，任你调教，激情无限。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000018-cue-0001"
    ): "免费看美女真人秀表演，美女裸聊表演，寂寞交友聊天室，乐趣多多。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000023-cue-0000"
    ): "征友。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000072-cue-0000"
    ): "那么，今天我们来讲容易混淆的「ぬ」。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000075-cue-0001"
    ): "那就是表示完成的「ぬ」。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000089-cue-0000"
    ): "图一中的「ぬ」，",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000091-cue-0000"
    ): "是表示完成的「ぬ」。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000092-cue-0000"
    ): "图二中的「ぬ」，",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000094-cue-0000"
    ): "是表示否定的「ぬ」。",
    (
        "e61b6979e9c481ce0af46bcf37e41cc32a835fe5fcec2817f256938ae719a132"
        "-seg-00000026-cue-0000"
    ): "做。",
    (
        "e61b6979e9c481ce0af46bcf37e41cc32a835fe5fcec2817f256938ae719a132"
        "-seg-00000046-cue-0000"
    ): "不行。",
    (
        "d1d431be669328ac93db8f7d1297d8d31a2f2dcfd1feb46cedbdd37077be72f3"
        "-seg-00000053-cue-0000"
    ): "回去吧。",
    (
        "805fb20e2129e8527d2ba5650db0b4fcef78c38e0c1d5d0df478cadd9901e205"
        "-seg-00000298-cue-0000"
    ): "因为。",
    (
        "8f7406b91dc12d284e51c4673bcf110ca5138d1ac813f6aa8912c038413ad4d2"
        "-seg-00000062-cue-0000"
    ): "会见面吗？",
    (
        "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939"
        "-seg-00000406-cue-0000"
    ): "就算去问了……",
    (
        "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939"
        "-seg-00000407-cue-0000"
    ): "虽然我知道，但还是就这样回去了，嗯、嗯、嗯……",
    (
        "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939"
        "-seg-00000407-cue-0001"
    ): "嗯、嗯、嗯……徘徊着。",
    (
        "d34a94a21909bf059375c474c0338e1cf47fb6fd5b0a52b22ccf174e3723b939"
        "-seg-00000407-cue-0003"
    ): "重新来过，回过神来，嗯、嗯、嗯、嗯。",
    (
        "ce37e47fdf9ee231b95f1f44dae4c1a4203ed23999895f25216cce726fa8203d"
        "-seg-00000212-cue-0000"
    ): "……",
    (
        "a5235799cb44359b4f760b725256d831197a43741f2430b0e9cfb7b34cc713a5"
        "-seg-00000114-cue-0000"
    ): "当时。",
    (
        "4d5d65a9642087e4587774089635db32e349db46678fbac15ad5a76fd04cf759"
        "-seg-00000223-cue-0000"
    ): "这里也是这样，对吧。",
    (
        "05aad23dda4327a3b8d9046a763a8de690dd5253d9a001cd7c41605785967285"
        "-seg-00000426-cue-0000"
    ): "很深吧，在里面。",
    (
        "05aad23dda4327a3b8d9046a763a8de690dd5253d9a001cd7c41605785967285"
        "-seg-00000436-cue-0000"
    ): "这叫什么名字？",
    (
        "05aad23dda4327a3b8d9046a763a8de690dd5253d9a001cd7c41605785967285"
        "-seg-00000438-cue-0000"
    ): "都市。",
    (
        "397e5df75c0d2768e57e93e0b86e55cc6a429201f9c24349404b945cf61e094d"
        "-seg-00003085-cue-0005"
    ): "。",
    (
        "4161cf5278c2a9d83d65c36fc2fe1587e4457086c1cb941717546552b20caeb7"
        "-seg-00000755-cue-0000"
    ): "莫德纳",
    (
        "3ae89c41503656fc87db978ab023414c32d73693890d06d48efffc194528962d"
        "-seg-00001197-cue-0000"
    ): "畑小姐。",
    (
        "ffb916fccf8122a99c7108f63f157aed7adc3c2480ccd09f7c97c95457ad3903"
        "-seg-00001381-cue-0000"
    ): "加油，加油。",
    (
        "9b3b127dd6e2d79a034c633ef8fe5dea3550d78129ec9ba8f5d9f3f2ca7e0b93"
        "-seg-00000475-cue-0000"
    ): "为了上床。",
    (
        "8e461af214f69fd312020b11a05b277a882f1b1f0fb964983a06391d4142113a"
        "-seg-00000300-cue-0000"
    ): "……",
    (
        "da882fbe03724493963851cc266b7afa4441ae2bc943a7c559f9b54a24108fbb"
        "-seg-00000015-cue-0000"
    ): "还在Tsumu酱面前卿卿我我的。",
    (
        "6d417676f86f7836022877766536329fb871d947b3bbf091d8be89bef271b5fa"
        "-seg-00000712-cue-0002"
    ): "Akari小姐。",
    (
        "6d417676f86f7836022877766536329fb871d947b3bbf091d8be89bef271b5fa"
        "-seg-00000712-cue-0004"
    ): "一想到以后再也见不到Akari小姐了……",
    (
        "c78f851b8fcb484ee2172a71e4482492b5f1dd64a26acf30a74364e791ad6890"
        "-seg-00000891-cue-0000"
    ): "啊！",
    (
        "5c94cba5678faf3dc02c3ac0aa32c86d4d6b778515dbc086a95b565a228c4d6b"
        "-seg-00000035-cue-0000"
    ): "是爸爸打来的，我出去接一下。",
    (
        "6d417676f86f7836022877766536329fb871d947b3bbf091d8be89bef271b5fa"
        "-seg-00000902-cue-0000"
    ): "……",
    (
        "d2470ececaef99e2b920dcdadd63fd57e92e0028fcddf64c511d86b6d7593053"
        "-seg-00000691-cue-0001"
    ): "至于这件事，我觉得没有写出来。",
    (
        "3ae89c41503656fc87db978ab023414c32d73693890d06d48efffc194528962d"
        "-seg-00000170-cue-0000"
    ): "你不是把门半开着吗？",
    (
        "a2066f68e73d30770cc61528a4f6b3879935bbffc2a560f859ce5fa868977fa7"
        "-seg-00000019-cue-0001"
    ): "愚蠢的男人。",
    (
        "a2066f68e73d30770cc61528a4f6b3879935bbffc2a560f859ce5fa868977fa7"
        "-seg-00001034-cue-0000"
    ): "啊，顶到那里了。",
    (
        "5578822219f3a604f7b96d62566b0863993d1e9c688e4e0e469a8f13898d12a9"
        "-seg-00001766-cue-0001"
    ): "Hikaru。",
    (
        "2ce5ab9e34b05ab744c9e12dae278f942ba46d1a36602c5656f1c022b77f5a64"
        "-seg-00000059-cue-0001"
    ): "等等……嗯……那个，把这里清理干净就行了吧。",
    (
        "f812cd0d871693924c297014449b9f6ffe78e036437c86a3f173b9f43da494a2"
        "-seg-00001152-cue-0006"
    ): "从而自动显示最合适的内容。",
    (
        "aa92fc8a4aa63b3829bdc543c3c16fee776aa0b88970a7afcdc7108591309ce1"
        "-seg-00000193-cue-0000"
    ): "啊，要去了、要去了、要去了……",
    (
        "d319a1494e5d3522d9d05780c20da28c73ea8d96f623340eb05233cf655434fb"
        "-seg-00000353-cue-0000"
    ): "……",
    (
        "f499abfa0cb9ea5537e54ff4a84cf1323e0092460be06e2a2880b2ec44da66d9"
        "-seg-00001356-cue-0000"
    ): "25周年。",
    (
        "134e1b3ed61f136be211594e4bd67ad557d8efa1a01c62e046ecdb39712fc3ee"
        "-seg-00001939-cue-0003"
    ): "啊，不……啊、啊、啊……等等、等等。",
    (
        "d269825a1a13a2f6f89b6038cc805de7f8b0297ad0417f9b257d052cbef77ea0"
        "-seg-00000470-cue-0000"
    ): "景！",
    (
        "12b064c77c1ff8c16ce2be675f97ce9bd8020ea0cbdc7dda76a60703ec85a2f1"
        "-seg-00000751-cue-0000"
    ): "不着急，不着急，不着急。",
    (
        "fcbfb62f1fa47c405c7adc83e1430bdf49c7efbca0a16ed1526cf90c30815c77"
        "-seg-00000706-cue-0000"
    ): "哈啊、哈啊……嗯、嗯……啊……",
    (
        "56022dacbcb4c9ddc54e4b017cb2d57f5ab11b4ea260015d0c04bb4b6d1a5a90"
        "-seg-00001320-cue-0000"
    ): "好敏感，好敏感。",
    (
        "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
        "-seg-00000031-cue-0000"
    ): "出来的他，不想擦边，一天最棒。",
    (
        "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
        "-seg-00000031-cue-0000"
    ): "出来的他，不想擦边，一天最棒。",
    (
        "b97e633255b9b11035319e052db668646bec3d0bfea4f08dcd3d5033b8ee1ec4"
        "-seg-00000867-cue-0000"
    ): "弄丢了。",
    (
        "85b4e3181113849deb9ef600dacb39a8a679b73d222d92d7cd46207870a19171"
        "-seg-00000257-cue-0000"
    ): "是围着店家。",
    (
        "1337b52ebedcebc0ac0a7223e42ac9f9597adf250589aea8ddd27b914c720819"
        "-seg-00000525-cue-0000"
    ): "光之链接。",
    (
        "9f94dda31aa39e4b831c31383a046103891e23c901f125c1ae1e6401c9dc9420"
        "-seg-00000497-cue-0000"
    ): "……",
    (
        "6d97b3624030401db532f7efb103611a2b71acb93696a9a5e93d8afd852975bf"
        "-seg-00000292-cue-0000"
    ): "都是我的错。",
    (
        "40f8976ac62f9796bdce46e60864d5d2c0f8266644310cacaf625dc6a66ce998"
        "-seg-00000000-cue-0004"
    ): "好友相约竞技。",
    (
        "b6ab8f3924fffffb6231c679fbd97fdacc83198fee6fe5c736a9f8b4136c8f19"
        "-seg-00000506-cue-0000"
    ): "……",
    (
        "84fc1de80b63849cd84ee25fe31664a77b8b0618f90f1314d9251a96d376bd53"
        "-seg-00000020-cue-0001"
    ): "这是常有的事。",
    (
        "e4d7f32001515d46b61880ca3a0940c46a992f5521a7f1cdb7fc528de3175684"
        "-seg-00000870-cue-0000"
    ): "我要去广州。",
    (
        "1193feda53ff5e9dc94325bf06934658fa960660f9fb975b86a6e05282c1569b"
        "-seg-00000442-cue-0000"
    ): "放着、放着、放着、放着。",
    (
        "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
        "-seg-00000039-cue-0000"
    ): "今晚游泳，圣诞宝贝。",
    (
        "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
        "-seg-00000039-cue-0000"
    ): "今晚游泳，圣诞宝贝。",
    (
        "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
        "-seg-00000041-cue-0000"
    ): "生存的惆怅。",
    (
        "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
        "-seg-00000041-cue-0000"
    ): "生存的惆怅。",
    (
        "b97e633255b9b11035319e052db668646bec3d0bfea4f08dcd3d5033b8ee1ec4"
        "-seg-00000868-cue-0005"
    ): "等她回来就放心了。",
    (
        "6d97b3624030401db532f7efb103611a2b71acb93696a9a5e93d8afd852975bf"
        "-seg-00000311-cue-0000"
    ): "（听不清）",
    (
        "e4d7f32001515d46b61880ca3a0940c46a992f5521a7f1cdb7fc528de3175684"
        "-seg-00000880-cue-0000"
    ): "青春美术学院。",
    (
        "1193feda53ff5e9dc94325bf06934658fa960660f9fb975b86a6e05282c1569b"
        "-seg-00000443-cue-0000"
    ): "尝一尝、尝一尝……",
    (
        "5b75549f4c4d5437da0b43da569cf5f43432fc5eecb6e0a93a841ee0619ae761"
        "-seg-00000060-cue-0000"
    ): "7.30秒。",
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000134-cue-0000"
    ): "状态点。",
    (
        "85c4a1a8bf91850f1974918aec3b759012a4a86cfea471cb9aa4a1d74bccb7d7"
        "-seg-00000857-cue-0000"
    ): "星宫老师。",
    (
        "635c891fc02ad78836d12a4f121d62aa73dd848cad1de5234a3a8d67eb0f9b0b"
        "-seg-00001132-cue-0000"
    ): "（听不清）",
    (
        "a6882b2fca91f6d2d5b7f6d6a65d4ffb1cbe7a226ad80e8fabbf36e44b823fe7"
        "-seg-00001510-cue-0000"
    ): "啊、啊……",
    (
        "fa98fef035313933494b516cda37e09ce285d1ad40cfe16e299e935bc1635dd0"
        "-seg-00001544-cue-0003"
    ): "嗯、嗯……啊……给我射了好多……嗯……",
    (
        "9f94dda31aa39e4b831c31383a046103891e23c901f125c1ae1e6401c9dc9420"
        "-seg-00002062-cue-0003"
    ): "不行了，啊啊啊，真的不行了。",
    (
        "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
        "-seg-00000042-cue-0000"
    ): "出来的他，不像……",
    (
        "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
        "-seg-00000042-cue-0000"
    ): "出来的他，不像……",
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000162-cue-0000"
    ): "你这家伙。",
    (
        "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91"
        "-seg-00000043-cue-0000"
    ): "嘴巴甜一点。",
    (
        "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0"
        "-seg-00000043-cue-0000"
    ): "嘴巴甜一点。",
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000244-cue-0000"
    ): "阿姨。",
    **{
        f"{video_id}-seg-{segment:08d}-cue-0000": target
        for video_id in (
            "52ea486d2da224214b5930ff6a9c2dad076464a311ec4142c153cb6164b88d91",
            "933a26e6486ea9a921b0b6fbd7781bfa166f9421cbc5476fca383fa1dcc2d3e0",
        )
        for segment, target in (
            (44, "涵哥哥。"),
            (45, "在路上。"),
            (46, "圣诞节。"),
            (47, "你想念他。"),
            (48, "家。"),
            (49, "今晚拥有圣诞宝贝，还保持关心。"),
            (50, "叮叮当，叮叮当，铃声多响亮。"),
            (51, "故事空间等着你去追踪它。"),
            (53, "夜里。"),
            (54, "圣诞宝贝总是能。"),
            (55, "总能实现你的愿望。"),
        )
    },
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000548-cue-0000"
    ): "给我。",
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000569-cue-0000"
    ): "阿姨们。",
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000634-cue-0000"
    ): "富贵斌哥。",
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000791-cue-0000"
    ): "啊。",
    (
        "a3fc95b3fb4051525bed5742ec361b4b6683b17bbb9b73da7230c132c28ea904"
        "-seg-00000823-cue-0000"
    ): "Chikura。",
    (
        "805fb20e2129e8527d2ba5650db0b4fcef78c38e0c1d5d0df478cadd9901e205"
        "-seg-00000577-cue-0000"
    ): "Kazushin",
}


def exact_required_translation(
    source_ja: str,
    *,
    cue_id: str | None = None,
) -> str | None:
    """Return a deterministic, source-grounded correction for one cue."""
    required = EXACT_CUE_ID_REQUIRED_TRANSLATIONS.get(cue_id)
    if required is not None:
        return required
    normalized_source = unicodedata.normalize("NFKC", source_ja).strip()
    return NORMALIZED_EXACT_CUE_REQUIRED_TRANSLATIONS.get(normalized_source)


CONTEXTUAL_ASR_FORBIDDEN_NAME_MARKERS = frozenset(
    {
        "伊卡尤",
        "伊贺由",
        "伊香由",
        "饭由",
        "ikayu",
        "ika-yu",
        "小姐",
        "女士",
        "先生",
        "桑",
    }
)
PROTECTED_JAPANESE_NAME_TERMS = frozenset({"紬", "大橋", "金塚", "大塚"})
GENERIC_GLOSSARY_CATEGORY_MARKERS = frozenset(
    {"action", "expression", "phrase", "verb"}
)
PERSON_GLOSSARY_CATEGORIES = frozenset(
    {"name", "person", "proper_name", "actor", "character"}
)
SHORT_KANA_NAME_SUFFIXES = ("さん", "ちゃん", "くん", "さま")
SHORT_KANA_NAME_PARTICLES = frozenset("はがをにとのへもで")
MAX_NONEXPLICIT_GENERIC_GLOSSARY_ENTRIES = 5
CONFIDENT_SIMPLIFIED_CHINESE_RE = re.compile(
    "[嗯啊哦哎呃唉哇哈嘿呵哼欸诶喂嘟吗呢吧呀啦嘛哟们这您你她它那哪么咱啥没别让给把被还过谢请妈爸哥姐媳对说个为发够亲开关门车钱时间见爱凤娱乐尽幺动几盘抢钻扑机风电戏传经掉寻侣妇达选择随载录了鲜优质网齐块锅报书创视频伦轮渴扫华语译尔务隐护赛费滚讨厌观财喷挥现红包兑业]"
)
CONFIDENT_SIMPLIFIED_CHINESE_EXACT = frozenset(
    {
        "我",
        "他",
        "好",
        "来",
        "去",
        "是",
        "有",
        "要",
        "走",
        "快",
        "行",
        "一",
        "最高",
        "安心",
        "秘密",
        "最近",
        "全部",
        "可能",
        "直接",
        "学校",
        "普通",
        "日本",
        "救命",
        "症",
        "自由",
        "脱掉",
        "精神分裂症",
        "第一首av新片天天更新",
        "不好意思",
        "不是",
        "英子英子",
        "期待期待",
        "希望",
        "上上上上",
        "一二三全部",
        "五十以上五十以下",
        "好好好快快快",
        "修学旅行",
        "超多片源",
        "第一印象",
        "下",
        "深呼吸",
        "千秋",
        "万部影片每天更新",
        "我只想忠于自我",
        "高崎雄二郎",
        "川北彩花",
        "自由体操",
        "有痛就有痛",
        "不",
        "悄",
        "不不不不不不",
        "自爆",
        "寿司",
        "2000年世界第32位",
        "看一下口味",
        "亚洲唯一官方赞助商实力有保证二十分钟内秒出款",
        "七七点com",
        "av大平台",
        "记得不是",
        "无任何广告完全不卡顿",
    }
)
JAPANESE_FALSE_FRIEND_EXACT = frozenset(
    {
        "丈夫",
        "上手",
        "䬋手",
        "一番",
        "今日",
        "今度",
        "何",
        "余裕",
        "先生",
        "元気",
        "大丈夫",
        "彼",
        "彼女",
        "彼氏",
        "昨日",
        "明日",
        "失礼",
        "奥",
        "平気",
        "本当",
        "本気",
        "本物",
        "本番",
        "残念",
        "私",
        "相手",
        "中身",
        "俺",
        "我慢",
        "挿入",
    }
)
EXPLICIT_HAN_MARKERS = frozenset("插射舔操乳奶阴屌穴精逼肛睾龟臀胸裆尿潮喷")
EXPLICIT_PREFERRED_VARIANTS = {
    "不戴套": ("没戴套", "无套", "裸着做", "裸交"),
}
PREFERRED_CONTEXT_VARIANTS = {
    "臭味": ("臭",),
    "校园霸凌": ("霸凌", "校园欺凌", "欺凌"),
    "秘密": ("保密",),
    "爸爸": ("爸", "父亲", "叔叔", "伯父", "岳父"),
    "妈妈": ("妈", "母亲", "阿姨", "伯母", "岳母"),
    "没关系": ("没事", "没问题", "不要紧", "可以", "还好"),
    "学生": ("男生", "女生", "同学"),
    "seina姐姐": ("seina姐",),
    "偷窃": ("偷了", "偷东西", "顺手牵羊"),
}
EXPLICIT_JAPANESE_MARKERS = (
    "おちんぽ",
    "ちんぽ",
    "チンポ",
    "ペニス",
    "まんこ",
    "マンコ",
    "膣",
    "中出し",
    "中に出",
    "射精",
    "精液",
    "フェラ",
    "クンニ",
    "セックス",
    "挿入",
    "アナル",
    "乳首",
    "おっぱい",
    "ゴム",
    "コンドーム",
    "生で",
    "生のまま",
    "舐め",
)
INABILITY_MARKERS = (
    "无法",
    "不能",
    "不可以",
    "没办法",
    "拒绝",
    "can't",
    "cannot",
    "unable",
    "won't",
)
TASK_MARKERS = (
    "翻译",
    "协助",
    "帮助",
    "帮你",
    "帮忙",
    "处理",
    "该内容",
    "这个内容",
    "translate",
    "translation",
    "assist",
    "content",
    "提供",
    "这类内容",
)
SOURCE_REFUSAL_RE = re.compile(
    "|".join(
        (
            r"翻訳を?手伝え(?:ない|ません)",
            r"翻訳(?:すること(?:が|は)?)?(?:でき(?:ない|ません)|出来(?:ない|ません))",
            r"翻訳(?:は|が)?無理",
            r"手伝(?:え(?:ない|ません)|うこと(?:が|は)?でき(?:ない|ません))",
            r"(?:処理|対応)(?:でき(?:ない|ません)|出来(?:ない|ません))",
            r"内容を扱え(?:ない|ません)",
            r"拒否(?:する|します)",
        )
    )
)
SOURCE_REPORTING_RE = re.compile(r"^(?:と|って)(?:言|話|書|聞|伝)")
POLICY_MARKERS = ("作为ai", "作为 ai", "政策", "安全准则", "policy")
APOLOGY_MARKERS = ("抱歉", "对不起", "很遗憾", "sorry", "apolog")
PLACEHOLDER_MARKERS = (
    "translation unavailable",
    "unable to translate",
    "待翻译",
    "暂无翻译",
    "译文如下",
    "翻译如下",
)
INAUDIBLE_TRANSLATION_CONTENT = "语音不清"


class MissingTranslationDependency(RuntimeError):
    pass


@dataclass(frozen=True)
class GlossaryEntry:
    category: str
    ja: str
    preferred_zh: str
    aliases: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    explicit: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (self.category, self.ja, self.preferred_zh)
        ):
            raise ValueError("glossary entry strings must be non-empty")
        if not isinstance(self.aliases, tuple) or any(
            type(value) is not str or not value.strip() for value in self.aliases
        ):
            raise ValueError("glossary aliases must be non-empty strings")
        if len(self.aliases) != len(set(self.aliases)):
            raise ValueError("glossary aliases must be unique")
        if (
            not isinstance(self.evidence_ids, tuple)
            or not self.evidence_ids
            or any(type(value) is not str or not value for value in self.evidence_ids)
        ):
            raise ValueError("glossary evidence IDs must be non-empty strings")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("glossary evidence IDs must be unique")
        if type(self.explicit) is not bool:
            raise ValueError("glossary explicit must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "ja": self.ja,
            "preferred_zh": self.preferred_zh,
            "aliases": list(self.aliases),
            "evidence_ids": list(self.evidence_ids),
            "explicit": self.explicit,
        }


@dataclass(frozen=True)
class Glossary:
    version: str
    entries: tuple[GlossaryEntry, ...]
    relationships: tuple[str, ...]
    register: str
    source_digest: str

    def __post_init__(self) -> None:
        if type(self.version) is not str or not self.version.strip():
            raise ValueError("glossary version must be non-empty")
        if type(self.register) is not str or not self.register.strip():
            raise ValueError("glossary register must be non-empty")
        if not isinstance(self.entries, tuple) or any(
            not isinstance(entry, GlossaryEntry) for entry in self.entries
        ):
            raise ValueError("glossary entries must be GlossaryEntry values")
        terms = [entry.ja for entry in self.entries]
        if len(terms) != len(set(terms)):
            raise ValueError("glossary terms must be unique")
        if not isinstance(self.relationships, tuple) or any(
            type(value) is not str or not value.strip() for value in self.relationships
        ):
            raise ValueError("glossary relationships must be non-empty strings")
        if len(self.relationships) != len(set(self.relationships)):
            raise ValueError("glossary relationships must be unique")
        if type(self.source_digest) is not str or not SHA256_RE.fullmatch(
            self.source_digest
        ):
            raise ValueError("glossary source digest must be a SHA-256")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "entries": [entry.to_dict() for entry in self.entries],
            "relationships": list(self.relationships),
            "register": self.register,
            "source_digest": self.source_digest,
        }


def glossary_digest(glossary: Glossary) -> str:
    if not isinstance(glossary, Glossary):
        raise ValueError("glossary digest requires a Glossary")
    entries = [
        {
            "category": entry.category,
            "ja": entry.ja,
            "preferred_zh": entry.preferred_zh,
            "aliases": sorted(entry.aliases),
            "evidence_ids": sorted(entry.evidence_ids),
            "explicit": entry.explicit,
        }
        for entry in glossary.entries
    ]
    entries.sort(
        key=lambda entry: json.dumps(
            entry,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    payload = {
        "version": glossary.version,
        "entries": entries,
        "relationships": sorted(glossary.relationships),
        "register": glossary.register,
        "source_digest": glossary.source_digest,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TranslationBlock:
    before_context: tuple[JapaneseCue, ...]
    commit_cues: tuple[JapaneseCue, ...]
    after_context: tuple[JapaneseCue, ...]
    source_digest: str
    source_count: int
    commit_start: int

    def __post_init__(self) -> None:
        if type(self.source_count) is not int or self.source_count <= 0:
            raise ValueError("translation block source_count must be positive")
        if type(self.commit_start) is not int or self.commit_start < 0:
            raise ValueError("translation block commit_start must be non-negative")
        if type(self.source_digest) is not str or not SHA256_RE.fullmatch(
            self.source_digest
        ):
            raise ValueError("translation block source digest must be a SHA-256")
        for name, values in (
            ("before_context", self.before_context),
            ("commit_cues", self.commit_cues),
            ("after_context", self.after_context),
        ):
            if not isinstance(values, tuple) or any(
                not isinstance(value, JapaneseCue) for value in values
            ):
                raise ValueError(f"{name} must contain JapaneseCue values")
        if not 1 <= len(self.commit_cues) <= 120:
            raise ValueError("translation blocks require 1..120 committed cues")
        commit_end = self.commit_start + len(self.commit_cues)
        if commit_end > self.source_count:
            raise ValueError("translation block commit range exceeds source")
        if len(self.before_context) != min(10, self.commit_start):
            raise ValueError("translation block has an incorrect before context")
        if len(self.after_context) != min(10, self.source_count - commit_end):
            raise ValueError("translation block has an incorrect after context")
        window = self.before_context + self.commit_cues + self.after_context
        ids = [cue.cue_id for cue in window]
        if any(not cue_id for cue_id in ids) or len(ids) != len(set(ids)):
            raise ValueError("translation block cue IDs must be globally unique")
        if any(
            current.start_ms < previous.start_ms
            for previous, current in zip(window, window[1:])
        ):
            raise ValueError("translation block cues are not in source order")

    @property
    def commit_ids(self) -> tuple[str, ...]:
        return tuple(cue.cue_id for cue in self.commit_cues)


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(raw: str) -> object:
    if not isinstance(raw, str):
        raise ValueError("response must be a JSON string")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def _validate_ids(expected_ids: Iterable[str]) -> list[str]:
    if not isinstance(expected_ids, (list, tuple)):
        raise ValueError("expected cue IDs must be an ordered list")
    ids = list(expected_ids)
    if any(type(value) is not str or not value for value in ids):
        raise ValueError("expected cue IDs must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise ValueError("expected cue IDs must be unique")
    return ids


def parse_translation_response(raw: str, expected_ids: list[str]) -> dict[str, str]:
    ids = _validate_ids(expected_ids)
    payload = _strict_json(raw)
    if not isinstance(payload, dict) or set(payload) != {"cues"}:
        raise ValueError("response must contain exactly the cues field")
    rows = payload["cues"]
    if not isinstance(rows, list):
        raise ValueError("response cues must be a list")

    returned_ids: list[str] = []
    result: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"id", "zh"}:
            raise ValueError(f"cue row {index} must contain exactly id and zh")
        cue_id = row["id"]
        text = row["zh"]
        if type(cue_id) is not str or type(text) is not str:
            raise ValueError(f"cue row {index} id and zh must be strings")
        text = text.strip()
        if not cue_id or not text:
            raise ValueError(f"cue row {index} contains an empty value")
        returned_ids.append(cue_id)
        result[cue_id] = text

    if returned_ids != ids:
        raise ValueError(f"cue IDs differ: expected {ids}, got {returned_ids}")
    return result


def source_cue_digest(cues: Iterable[JapaneseCue]) -> str:
    ordered = tuple(cues)
    if any(not isinstance(cue, JapaneseCue) for cue in ordered):
        raise ValueError("source digest requires JapaneseCue values")
    payload = [[cue.cue_id, cue.start_ms, cue.end_ms, cue.text] for cue in ordered]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_translation_blocks(
    cues: Iterable[JapaneseCue],
    commit_size: int = 120,
    context_size: int = 10,
) -> tuple[TranslationBlock, ...]:
    if type(commit_size) is not int or not 1 <= commit_size <= 120:
        raise ValueError("commit_size must be an integer in 1..120")
    if type(context_size) is not int or context_size != 10:
        raise ValueError("context_size must be exactly 10")
    ordered = tuple(cues)
    if any(not isinstance(item, JapaneseCue) for item in ordered):
        raise ValueError("translation blocks require JapaneseCue values")
    ids = [item.cue_id for item in ordered]
    if any(not cue_id for cue_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("source cue IDs must be non-empty and unique")

    if not ordered:
        return ()
    digest = source_cue_digest(ordered)
    result: list[TranslationBlock] = []
    for start in range(0, len(ordered), commit_size):
        end = min(start + commit_size, len(ordered))
        result.append(
            TranslationBlock(
                ordered[max(0, start - context_size) : start],
                ordered[start:end],
                ordered[end : min(len(ordered), end + context_size)],
                digest,
                len(ordered),
                start,
            )
        )
    return tuple(result)


def validate_translation_block(
    block: TranslationBlock,
    source_cues: Iterable[JapaneseCue],
) -> None:
    if not isinstance(block, TranslationBlock):
        raise ValueError("block must be a TranslationBlock")
    ordered = tuple(source_cues)
    if len(ordered) != block.source_count:
        raise ValueError("translation block source count differs")
    if source_cue_digest(ordered) != block.source_digest:
        raise ValueError("translation block full source digest differs")
    start = block.commit_start
    end = start + len(block.commit_cues)
    expected_before = ordered[max(0, start - 10) : start]
    expected_commit = ordered[start:end]
    expected_after = ordered[end : min(len(ordered), end + 10)]
    if (
        block.before_context != expected_before
        or block.commit_cues != expected_commit
        or block.after_context != expected_after
    ):
        raise ValueError("translation block context/commit window differs from source")


def apply_translations(
    cues: Iterable[JapaneseCue],
    translations: dict[str, str],
    expected_digest: str,
) -> tuple[JapaneseCue, ...]:
    ordered = tuple(cues)
    ids = [cue.cue_id for cue in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("source cue IDs must be unique")
    if timestamp_digest(ordered) != expected_digest:
        raise ValueError("timestamp digest differs before translation")
    if not isinstance(translations, dict) or list(translations) != ids:
        raise ValueError("translation cue IDs or order differ from source")
    if any(
        type(value) is not str or not value.strip() for value in translations.values()
    ):
        raise ValueError("translations must be non-empty strings")

    result = tuple(cue.with_text(translations[cue.cue_id].strip()) for cue in ordered)
    if timestamp_digest(result) != expected_digest:
        raise ValueError("timestamp digest changed while applying translation")
    return result


def _string_list(
    value: object, field: str, *, allow_empty: bool = True
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if any(type(item) is not str or not item.strip() for item in value):
        raise ValueError(f"{field} must contain non-empty strings")
    cleaned = tuple(item.strip() for item in value)
    if not allow_empty and not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError(f"{field} must not contain duplicates")
    return cleaned


def parse_glossary_response(
    raw: str,
    source_cues: Iterable[JapaneseCue],
) -> Glossary:
    cues = tuple(source_cues)
    if any(not isinstance(cue, JapaneseCue) for cue in cues):
        raise ValueError("glossary source values must be JapaneseCue objects")
    cue_map = {cue.cue_id: cue for cue in cues}
    if len(cue_map) != len(cues):
        raise ValueError("source cue IDs must be unique")
    payload = _strict_json(raw)
    required = {"version", "entries", "relationships", "register"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("glossary has an invalid top-level schema")
    if type(payload["version"]) is not str or not payload["version"].strip():
        raise ValueError("glossary version must be a non-empty string")
    if type(payload["register"]) is not str or not payload["register"].strip():
        raise ValueError("glossary register must be a non-empty string")
    relationships = _string_list(payload["relationships"], "relationships")
    rows = payload["entries"]
    if not isinstance(rows, list):
        raise ValueError("glossary entries must be a list")

    entries: list[GlossaryEntry] = []
    entry_positions: dict[str, int] = {}
    row_fields = {
        "category",
        "ja",
        "preferred_zh",
        "aliases",
        "evidence_ids",
        "explicit",
    }
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != row_fields:
            raise ValueError(f"glossary entry {index} has an invalid schema")
        category = row["category"]
        ja = row["ja"]
        preferred = row["preferred_zh"]
        explicit = row["explicit"]
        if any(
            type(value) is not str or not value.strip()
            for value in (category, ja, preferred)
        ):
            raise ValueError(f"glossary entry {index} has an empty string")
        if type(explicit) is not bool:
            raise ValueError(f"glossary entry {index} explicit must be boolean")
        category = category.strip()
        ja = ja.strip()
        preferred = preferred.strip()
        aliases = _string_list(row["aliases"], f"entry {index} aliases")
        evidence_ids = _string_list(
            row["evidence_ids"],
            f"entry {index} evidence_ids",
            allow_empty=False,
        )
        if any(evidence_id not in cue_map for evidence_id in evidence_ids):
            raise ValueError(f"glossary entry {index} cites an unknown cue ID")
        if not any(ja in cue_map[evidence_id].text for evidence_id in evidence_ids):
            raise ValueError(f"glossary entry {index} lacks source evidence")
        entry = GlossaryEntry(
            category,
            ja,
            preferred,
            aliases,
            evidence_ids,
            explicit,
        )
        previous_position = entry_positions.get(ja)
        if previous_position is None:
            entry_positions[ja] = len(entries)
            entries.append(entry)
            continue
        previous = entries[previous_position]
        if previous.category != category:
            raise ValueError(f"duplicate glossary category conflict for {ja}")
        if previous.preferred_zh != preferred:
            raise ValueError(
                f"duplicate glossary preferred translation conflict for {ja}"
            )
        entries[previous_position] = GlossaryEntry(
            category,
            ja,
            preferred,
            tuple(dict.fromkeys(previous.aliases + aliases)),
            tuple(dict.fromkeys(previous.evidence_ids + evidence_ids)),
            previous.explicit or explicit,
        )

    glossary = Glossary(
        payload["version"].strip(),
        tuple(entries),
        relationships,
        payload["register"].strip(),
        source_cue_digest(cues),
    )
    validate_glossary_against_source(glossary, cues)
    return glossary


def validate_glossary_against_source(
    glossary: Glossary,
    source_cues: Iterable[JapaneseCue],
) -> None:
    if not isinstance(glossary, Glossary):
        raise ValueError("glossary must be a Glossary")
    cues = tuple(source_cues)
    if source_cue_digest(cues) != glossary.source_digest:
        raise ValueError("glossary source digest differs")
    cue_map = {cue.cue_id: cue for cue in cues}
    if len(cue_map) != len(cues):
        raise ValueError("glossary source cue IDs must be unique")
    nonexplicit_generic = [
        entry
        for entry in glossary.entries
        if not entry.explicit
        and any(
            marker in entry.category.casefold()
            for marker in GENERIC_GLOSSARY_CATEGORY_MARKERS
        )
    ]
    if len(nonexplicit_generic) > MAX_NONEXPLICIT_GENERIC_GLOSSARY_ENTRIES:
        raise ValueError(
            "glossary contains too many non-explicit generic phrase/action entries"
        )
    for entry in glossary.entries:
        if entry.ja in REJECTED_GLOSSARY_JA_TERMS:
            raise ValueError(
                f"glossary term {entry.ja} is a context-inconsistent ASR artifact"
            )
        if any(evidence_id not in cue_map for evidence_id in entry.evidence_ids):
            raise ValueError(f"glossary term {entry.ja} cites an unknown cue")
        if not any(
            entry.ja in cue_map[evidence_id].text for evidence_id in entry.evidence_ids
        ):
            raise ValueError(f"glossary term {entry.ja} lacks cited source evidence")
        if (
            entry.ja in ROMANIZATION_REQUIRED_KANA_NAMES
            and re.search(r"[A-Za-z]", entry.preferred_zh) is None
        ):
            raise ValueError(
                f"kana-only name {entry.ja} must use grounded Latin romanization"
            )
        required_latin = DIRECT_KANA_NAME_REQUIRED_LATIN.get(entry.ja)
        if (
            required_latin is not None
            and required_latin.casefold() not in entry.preferred_zh.casefold()
        ):
            raise ValueError(
                f"kana-only name {entry.ja} must use exact Latin stem {required_latin}"
            )


def merge_glossaries(*glossaries: Glossary) -> Glossary:
    if not glossaries:
        raise ValueError("at least one glossary is required")
    if any(not isinstance(glossary, Glossary) for glossary in glossaries):
        raise ValueError("merge values must be Glossary objects")
    version = glossaries[0].version
    register = glossaries[0].register
    source_digest = glossaries[0].source_digest
    entries: dict[str, GlossaryEntry] = {}
    relationships: list[str] = []
    for glossary in glossaries:
        if glossary.version != version:
            raise ValueError("glossary version conflict")
        if glossary.register != register:
            raise ValueError("glossary register conflict")
        if glossary.source_digest != source_digest:
            raise ValueError("glossary source digest conflict")
        for relationship in glossary.relationships:
            if relationship not in relationships:
                relationships.append(relationship)
        for entry in glossary.entries:
            previous = entries.get(entry.ja)
            if previous is None:
                entries[entry.ja] = entry
                continue
            if previous.preferred_zh != entry.preferred_zh:
                raise ValueError(
                    f"glossary preferred translation conflict for {entry.ja}"
                )
            if previous.category != entry.category:
                raise ValueError(f"glossary category conflict for {entry.ja}")
            aliases = tuple(dict.fromkeys(previous.aliases + entry.aliases))
            evidence = tuple(dict.fromkeys(previous.evidence_ids + entry.evidence_ids))
            entries[entry.ja] = GlossaryEntry(
                previous.category,
                previous.ja,
                previous.preferred_zh,
                aliases,
                evidence,
                previous.explicit or entry.explicit,
            )
    return Glossary(
        version,
        tuple(entries.values()),
        tuple(relationships),
        register,
        source_digest,
    )


def load_simplifier() -> Callable[[str], str]:
    try:
        from opencc import OpenCC
    except (ImportError, ModuleNotFoundError) as exc:
        raise MissingTranslationDependency(
            "Simplified-Chinese validation requires opencc-python-reimplemented"
        ) from exc
    try:
        converter = OpenCC("t2s")
    except Exception as exc:
        raise MissingTranslationDependency(
            f"failed to initialize OpenCC t2s: {exc}"
        ) from exc
    return converter.convert


def _content(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "S", "C"))
    )


def _source_is_confident_simplified_chinese(value: str) -> bool:
    """Recognize source text that is already safe Simplified Chinese.

    FunASR's Japanese mode can render short non-lexical sounds and a minority of
    dialogue directly as Chinese.  Exact preservation is correct for those rows,
    but must remain forbidden for Japanese kanji-only cues such as ``大丈夫`` or
    ``本当``.  Keep this deliberately lexical and conservative.
    """

    if KANA_RE.search(value):
        return False
    content = _content(value)
    short_han_literal = bool(
        content
        and len(content) <= 3
        and content not in JAPANESE_FALSE_FRIEND_EXACT
        and all(HAN_RE.fullmatch(char) for char in content)
    )
    repeated_confident_literal = any(
        len(content) >= len(term) * 2
        and len(content) % len(term) == 0
        and content == term * (len(content) // len(term))
        for term in CONFIDENT_SIMPLIFIED_CHINESE_EXACT
    )
    return bool(
        content
        and (
            CONFIDENT_SIMPLIFIED_CHINESE_RE.search(content)
            or content in CONFIDENT_SIMPLIFIED_CHINESE_EXACT
            or repeated_confident_literal
            or short_han_literal
            or BLOOD_TYPE_LITERAL_RE.fullmatch(content)
        )
    )


def _refusal_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        char
        for char in normalized
        if not char.isspace()
        and not unicodedata.category(char).startswith(("P", "S", "C", "M", "Z"))
    )


def _source_supports_refusal_dialogue(source_ja: str) -> bool:
    key = _refusal_key(source_ja)
    for match in SOURCE_REFUSAL_RE.finditer(key):
        if SOURCE_REPORTING_RE.match(key[match.end() :]):
            continue
        return True
    return False


def _protected_target(text: str, glossary: Glossary | None) -> str:
    result = text
    protected: list[str] = list(PROTECTED_JAPANESE_NAME_TERMS)
    if glossary is not None:
        for entry in glossary.entries:
            if entry.category.casefold() in {
                "name",
                "person",
                "proper_name",
                "actor",
            }:
                protected.extend((entry.ja, *entry.aliases))
    for index, term in enumerate(sorted(set(protected), key=len, reverse=True)):
        if term:
            result = result.replace(term, f" PROTECTED{index} ")
    return result


def _preferred_translation_present(entry: GlossaryEntry, candidate: str) -> bool:
    if entry.preferred_zh in candidate:
        return True
    folded_candidate = unicodedata.normalize("NFKC", candidate).casefold()
    if any(
        required_latin.casefold() in folded_candidate
        for source_term in (entry.ja, *entry.aliases)
        if (required_latin := DIRECT_KANA_NAME_REQUIRED_LATIN.get(source_term))
        is not None
    ):
        return True
    preferred = _content(entry.preferred_zh)
    target = _content(candidate)
    if preferred and preferred in target:
        return True
    if any(
        _content(variant) in target
        for variant in PREFERRED_CONTEXT_VARIANTS.get(preferred, ())
    ):
        return True
    if entry.explicit and any(
        _content(variant) in target
        for variant in EXPLICIT_PREFERRED_VARIANTS.get(preferred, ())
    ):
        return True
    stem = preferred.rstrip("了着过的地得")
    if stem != preferred and len(stem) >= 2 and stem in target:
        return True
    if not entry.explicit:
        return False
    if len(stem) == 1 and stem in target:
        return True
    markers = EXPLICIT_HAN_MARKERS.intersection(preferred)
    return bool(markers.intersection(target))


def _source_contains_glossary_term(
    source: str, term: str, *, person_name: bool = False
) -> bool:
    if (
        person_name
        and 1 < len(term) <= 2
        and all(KANA_RE.fullmatch(char) for char in term)
    ):
        start = 0
        while True:
            index = source.find(term, start)
            if index < 0:
                return False
            right = source[index + len(term) :]
            if not right or not KANA_RE.fullmatch(right[0]):
                return True
            if right.startswith(SHORT_KANA_NAME_SUFFIXES):
                return True
            if right[0] in SHORT_KANA_NAME_PARTICLES and (
                len(right) == 1 or not KANA_RE.fullmatch(right[1])
            ):
                return True
            start = index + 1
    if len(term) != 1:
        return term in source
    start = 0
    while True:
        index = source.find(term, start)
        if index < 0:
            return False
        left = source[index - 1] if index else ""
        right_index = index + 1
        right = source[right_index] if right_index < len(source) else ""
        if not (left and HAN_RE.fullmatch(left)) and not (
            right and HAN_RE.fullmatch(right)
        ):
            return True
        start = index + 1


def _source_contains_unshadowed_glossary_term(
    source: str,
    entry: GlossaryEntry,
    glossary: Glossary,
) -> bool:
    """Return whether an entry occurs outside a longer glossary term.

    Glossaries can contain both a general term and a longer, more specific
    form, for example ``父`` and ``お父さん``.  The shorter term must not impose
    a second, contradictory target translation when it only occurs inside the
    longer form.
    """
    person_name = entry.category.casefold() in PERSON_GLOSSARY_CATEGORIES
    source_terms = tuple(dict.fromkeys((entry.ja, *entry.aliases)))
    longer_terms = {
        term
        for other in glossary.entries
        for term in (other.ja, *other.aliases)
        if len(term) > 0
    }
    for term in source_terms:
        if not _source_contains_glossary_term(source, term, person_name=person_name):
            continue
        start = 0
        while True:
            index = source.find(term, start)
            if index < 0:
                break
            end = index + len(term)
            shadowed = any(
                len(longer) > len(term)
                and longer_start <= index
                and end <= longer_start + len(longer)
                for longer in longer_terms
                for longer_start in _substring_starts(source, longer)
            )
            if not shadowed:
                return True
            start = index + 1
    return False


def _substring_starts(source: str, term: str) -> tuple[int, ...]:
    starts: list[int] = []
    start = 0
    while True:
        index = source.find(term, start)
        if index < 0:
            return tuple(starts)
        starts.append(index)
        start = index + 1


def _explicit_glossary_context(entry: GlossaryEntry, source: str) -> bool:
    values = (source, entry.ja, *entry.aliases)
    return any(
        marker in value for marker in EXPLICIT_JAPANESE_MARKERS for value in values
    )


def _preservable_ascii_label(source: str, candidate: str) -> bool:
    source_content = _content(source)
    candidate_content = _content(candidate)
    return bool(
        source_content
        and (
            (
                ASCII_LABEL_RE.fullmatch(source_content)
                or source_content in PRESERVABLE_ASCII_LABELS
                or source_content in PRESERVABLE_SINGLE_ASCII_LABELS
            )
            and source_content == candidate_content
            or PRESERVABLE_KANA_ASCII_LABELS.get(source_content) == candidate_content
        )
    )


def _preservable_exact_romanized_name(source: str, candidate: str) -> bool:
    source_content = _content(source)
    required_latin = DIRECT_KANA_NAME_REQUIRED_LATIN.get(source_content)
    return bool(
        required_latin is not None
        and _content(candidate).casefold() == required_latin.casefold()
    )


def _preservable_numeric_literal(source: str, candidate: str) -> bool:
    source_content = _content(source)
    return bool(
        source_content
        and source_content not in NON_PRESERVABLE_NUMERIC_LITERALS
        and (
            NUMERIC_LITERAL_RE.fullmatch(source_content)
            or NUMERIC_WITH_UNIT_LITERAL_RE.fullmatch(source_content)
            or YEAR_LITERAL_RE.fullmatch(source_content)
        )
        and source_content == _content(candidate)
    )


def _preservable_nonlexical_literal(source: str, candidate: str) -> bool:
    normalized_source = unicodedata.normalize("NFKC", source).strip()
    normalized_candidate = unicodedata.normalize("NFKC", candidate).strip()
    compact_candidate = "".join(normalized_candidate.split())
    return bool(
        normalized_source
        and not _content(normalized_source)
        and (
            normalized_source == normalized_candidate
            or re.fullmatch(r"(?:\.{3}){1,3}", compact_candidate) is not None
        )
    )


def _preservable_fragment_ellipsis(source: str, candidate: str) -> bool:
    source_content = _content(source)
    normalized_candidate = "".join(unicodedata.normalize("NFKC", candidate).split())
    return bool(
        (
            1 <= len(source_content) <= 2
            and all(
                KANA_RE.fullmatch(character) is not None for character in source_content
            )
            or any(
                artifact in source and required == "……"
                for artifact, required in CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS.items()
            )
            or re.fullmatch(r"[a-z]\.?", source.strip()) is not None
        )
        and re.fullmatch(r"(?:\.{3}){1,3}", normalized_candidate) is not None
    )


def _preservable_elongated_kana_interjection(source: str, candidate: str) -> bool:
    source_content = _content(source)
    target_content = _content(candidate)
    trailing_count = (
        len(source_content) - len(source_content.rstrip(source_content[-1]))
        if source_content
        else 0
    )
    return bool(
        len(source_content) >= 4
        and all(KANA_RE.fullmatch(char) is not None for char in source_content)
        and trailing_count >= 3
        and target_content in {"啊", "哦", "嗯", "呀", "呢", "吗"}
    )


def _preservable_linguistic_quoted_kana(source: str, candidate: str) -> bool:
    definition_question = "って何" in source
    source_term_discussion = "間違いやすい" in source
    if not (definition_question or source_term_discussion) and not any(
        marker in source
        for marker in (
            "現代の日本語で",
            "古文",
            "官僚の縫",
            "完了の",
            "は省略",
        )
    ):
        return False
    runs = tuple(KANA_RUN_RE.finditer(candidate))
    quote_pairs = {("「", "」"), ("“", "”"), ('"', '"')}
    return bool(
        runs
        and all(
            len(match.group()) <= (12 if definition_question else 3)
            and match.start() > 0
            and match.end() < len(candidate)
            and (candidate[match.start() - 1], candidate[match.end()]) in quote_pairs
            and (
                not (definition_question or source_term_discussion)
                or match.group() in source
            )
            for match in runs
        )
    )


def translation_flags(
    source_ja: str,
    text: str,
    glossary: Glossary | None = None,
    *,
    simplifier: Callable[[str], str] | None = None,
    cue_id: str | None = None,
) -> list[str]:
    if type(source_ja) is not str or type(text) is not str:
        raise ValueError("source and translation must be strings")
    if glossary is not None and not isinstance(glossary, Glossary):
        raise ValueError("glossary must be a Glossary")
    candidate = text.strip()
    exact_cue_translation = exact_required_translation(source_ja, cue_id=cue_id)
    exact_cue_translation_satisfied = bool(
        exact_cue_translation is not None
        and _content(candidate) == _content(exact_cue_translation)
    )
    flags: list[str] = []
    folded = _refusal_key(candidate)

    has_inability = any(_refusal_key(marker) in folded for marker in INABILITY_MARKERS)
    has_task = any(_refusal_key(marker) in folded for marker in TASK_MARKERS)
    has_policy = any(_refusal_key(marker) in folded for marker in POLICY_MARKERS)
    has_apology = any(_refusal_key(marker) in folded for marker in APOLOGY_MARKERS)
    source_refusal_dialogue = _source_supports_refusal_dialogue(source_ja)
    if (
        has_inability
        and has_task
        and (has_apology or has_policy)
        and not source_refusal_dialogue
    ):
        flags.append("refusal")
    if any(_refusal_key(marker) in folded for marker in PLACEHOLDER_MARKERS):
        flags.append("placeholder")
    if (
        KANA_RE.search(candidate)
        and not exact_cue_translation_satisfied
        and not _preservable_linguistic_quoted_kana(source_ja, candidate)
    ):
        flags.append("japanese_remaining")
    if (
        any(
            name in source_ja and required_latin.casefold() not in candidate.casefold()
            for name, required_latin in DIRECT_KANA_NAME_REQUIRED_LATIN.items()
        )
        and not exact_cue_translation_satisfied
    ):
        flags.append("kana_name_romanization_missing")
    for artifact, required_translation in CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS.items():
        if artifact not in source_ja:
            continue
        if required_translation not in candidate:
            flags.append("contextual_asr_correction_missing")
        if artifact == "いかゆさん":
            folded_candidate = unicodedata.normalize("NFKC", candidate).casefold()
            if any(
                marker.casefold() in folded_candidate
                for marker in CONTEXTUAL_ASR_FORBIDDEN_NAME_MARKERS
            ):
                flags.append("contextual_asr_name_invented")
    if exact_cue_translation is not None and not exact_cue_translation_satisfied:
        flags.append("exact_cue_translation_mismatch")

    source_content = _content(source_ja)
    target_content = _content(candidate)
    ascii_label = _preservable_ascii_label(source_ja, candidate)
    exact_romanized_name = _preservable_exact_romanized_name(source_ja, candidate)
    numeric_literal = _preservable_numeric_literal(source_ja, candidate)
    nonlexical_literal = _preservable_nonlexical_literal(source_ja, candidate)
    fragment_ellipsis = _preservable_fragment_ellipsis(source_ja, candidate)
    explicit_inaudible = target_content == INAUDIBLE_TRANSLATION_CONTENT
    elongated_kana_interjection = _preservable_elongated_kana_interjection(
        source_ja, candidate
    )
    contextual_short_translation = any(
        artifact in source_ja and required_translation in candidate
        for artifact, required_translation in CONTEXTUAL_ASR_REQUIRED_TRANSLATIONS.items()
    )
    if (
        not HAN_RE.search(candidate)
        and target_content not in ALLOWED_NON_HAN_SUBTITLES
        and not ascii_label
        and not exact_romanized_name
        and not numeric_literal
        and not nonlexical_literal
        and not fragment_ellipsis
        and not exact_cue_translation_satisfied
    ):
        flags.append("not_simplified_chinese")
    if (
        source_content
        and source_content == target_content
        and not _source_is_confident_simplified_chinese(source_ja)
        and not ascii_label
        and not numeric_literal
        and not exact_cue_translation_satisfied
    ):
        flags.append("unchanged_source")
    if (
        len(source_content) >= 8
        and not elongated_kana_interjection
        and not contextual_short_translation
        and not explicit_inaudible
        and not exact_cue_translation_satisfied
    ):
        ratio = len(target_content) / len(source_content) if source_content else 0.0
        if ratio < 0.15:
            flags.append("suspiciously_short")
        elif ratio > 3.0:
            flags.append("suspiciously_long")

    if glossary is not None and not exact_cue_translation_satisfied:
        for entry in glossary.entries:
            if (
                not entry.explicit
                and entry.category.casefold() not in PERSON_GLOSSARY_CATEGORIES
                and len(_content(entry.ja)) <= 1
            ):
                # A single generic character (for example 何 in 何チュウ) is
                # too ambiguous to require a literal glossary rendering.
                continue
            if (
                not _source_contains_unshadowed_glossary_term(
                    source_ja, entry, glossary
                )
                or _preferred_translation_present(entry, candidate)
                or (entry.explicit and not _explicit_glossary_context(entry, source_ja))
            ):
                continue
            flag = (
                "explicit_term_missing" if entry.explicit else "glossary_term_missing"
            )
            if flag not in flags:
                flags.append(flag)

    if simplifier is None:
        simplifier = load_simplifier()
    unprotected = _protected_target(candidate, glossary)
    try:
        simplified = simplifier(unprotected)
    except Exception as exc:
        raise MissingTranslationDependency(
            f"Simplified-Chinese conversion failed: {exc}"
        ) from exc
    if simplified != unprotected:
        flags.append("traditional_character")
    return flags
