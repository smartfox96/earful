"""An agent that answers in Chinese, which is written without spaces, and is stopped part
way through its reply."""

from __future__ import annotations

import livekit.agents as lk

from earful.livekit import Case, ScriptedModel, UserSpeech, user_said
from shapes.corpus import judged

MENU = "今晚我们有烤南瓜汤作为开胃菜然后是柠檬黄油烤鲈鱼或者慢炖牛小排甜点是黑巧克力挞"


def case() -> Case:
    return Case(
        name="menu_in_chinese_interrupted",
        agent=lambda: lk.Agent(instructions="你是餐厅的接待员。"),
        speeches=[
            UserSpeech(start=0.5, end=2.5, transcript="请介绍一下菜单"),
            UserSpeech(start=4.0, end=5.0, transcript="停"),
        ],
        policy=ScriptedModel().when(user_said("停"), say="好的").otherwise(say=MENU),
        seconds_per_letter=0.15,
    )


SHOULD = judged(M1="pass", M2="pass")
DEFECT = judged(M1="unobservable", M2="rule c")  # the cut is by character, and words are compared
