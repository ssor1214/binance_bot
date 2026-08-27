"""그리드 격자 상태를 차트 이미지로 그린다.

텍스트 사다리(scalp_bot_e3.render_grid_view)는 격자가 많아지면 한눈에 안 들어온다.
사용자 요청("텍스트가 아닌 아예 차트를 들고올 순 없어?")에 따라 PNG 를 만들어
텔레그램 sendPhoto 로 보낸다.

한 장에 슬롯 여러 개를 세로로 쌓는다. 슬롯마다
  - 최근 가격 곡선
  - 격자선(칸 상태별 색)
  - 현재가 가로선
  - 범위 밴드
를 그린다.
"""
from __future__ import annotations

import io
import os
from typing import Iterable, Sequence

import matplotlib

matplotlib.use("Agg")           # 화면 없는 환경. import 순서를 지켜야 한다.
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402


def _use_korean_font() -> None:
    """한글 라벨이 깨지지 않게 시스템 폰트를 붙인다.

    matplotlib 기본 DejaVu Sans 에는 한글 글리프가 없어 전부 네모로 나온다.
    Windows 는 맑은 고딕이 기본 제공되므로 있으면 쓴다.
    """
    for path in ("C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/gulim.ttc",
                 "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"):
        try:
            if not os.path.exists(path):
                continue
            font_manager.fontManager.addfont(path)
            name = font_manager.FontProperties(fname=path).get_name()
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False   # 마이너스 기호 깨짐 방지
            return
        except Exception:
            continue


_use_korean_font()

# 칸 상태별 색. 텍스트 뷰의 기호와 뜻을 맞춘다.
#   보유(●)   매도대기(○)   매수대기(◇)   빈칸(·)
COLOR_HELD = "#e8804a"
COLOR_SELL = "#4a90d9"
COLOR_BUY = "#5fb87a"
COLOR_EMPTY = "#cfcfcf"
COLOR_PRICE = "#222222"
COLOR_MARK = "#d64545"


def _slot_axes(ax, title, prices, levels, held, buy_rungs, sell_rungs, mark_price):
    n = len(levels)
    if n:
        ax.axhspan(levels[0], levels[-1], color="#f2f6fa", zorder=0)

    for i, lv in enumerate(levels):
        if i in sell_rungs:
            c, lw = COLOR_SELL, 1.6
        elif i in held:
            c, lw = COLOR_HELD, 2.0
        elif i in buy_rungs:
            c, lw = COLOR_BUY, 1.6
        else:
            c, lw = COLOR_EMPTY, 0.8
        ax.axhline(lv, color=c, linewidth=lw, zorder=1)

    if prices:
        ax.plot(range(len(prices)), prices, color=COLOR_PRICE,
                linewidth=1.1, zorder=3)

    if mark_price:
        ax.axhline(mark_price, color=COLOR_MARK, linewidth=1.2,
                   linestyle="--", zorder=4)
        ax.annotate(f"{mark_price:.6g}",
                    xy=(1.0, mark_price), xycoords=("axes fraction", "data"),
                    xytext=(4, 0), textcoords="offset points",
                    color=COLOR_MARK, fontsize=8, va="center")

    ax.set_title(title, fontsize=9, loc="left")
    ax.tick_params(labelsize=7)
    ax.margins(x=0.02)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def render_grid_chart(
    slots: Sequence[dict],
    width: float = 8.0,
    height_per_slot: float = 2.6,
) -> bytes:
    """슬롯 목록을 PNG 바이트로 그린다.

    slots 각 원소는 다음 키를 갖는다.
        title       제목 문자열
        prices      최근 가격 리스트(비어 있어도 됨)
        levels      격자 가격 리스트
        held        보유 rung 집합/리스트
        buy_rungs   매수 대기 rung
        sell_rungs  매도 대기 rung
        mark_price  현재가
    """
    slots = list(slots) or [{"title": "표시할 슬롯이 없습니다", "prices": [],
                             "levels": [], "held": (), "buy_rungs": (),
                             "sell_rungs": (), "mark_price": 0.0}]
    fig, axes = plt.subplots(
        len(slots), 1,
        figsize=(width, max(height_per_slot, height_per_slot * len(slots))),
        squeeze=False,
    )
    for ax, slot in zip((a[0] for a in axes), slots):
        _slot_axes(
            ax,
            slot.get("title", ""),
            list(slot.get("prices") or []),
            list(slot.get("levels") or []),
            set(slot.get("held") or ()),
            set(slot.get("buy_rungs") or ()),
            set(slot.get("sell_rungs") or ()),
            float(slot.get("mark_price") or 0.0),
        )

    handles = [
        Line2D([], [], color=COLOR_HELD, lw=2.0, label="보유"),
        Line2D([], [], color=COLOR_SELL, lw=1.6, label="매도대기"),
        Line2D([], [], color=COLOR_BUY, lw=1.6, label="매수대기"),
        Line2D([], [], color=COLOR_EMPTY, lw=1.0, label="빈칸"),
        Line2D([], [], color=COLOR_MARK, lw=1.2, ls="--", label="현재가"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=5, fontsize=8,
               frameon=False, bbox_to_anchor=(0.5, 0.0))
    fig.tight_layout(rect=(0, 0.05, 1, 1))

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue()


def slot_payload(
    title: str,
    state: dict,
    mark_price: float,
    prices: Iterable[float] = (),
) -> dict:
    """CycleState 를 dict 로 저장한 형태를 차트 입력으로 바꾼다."""
    buy_orders = (state or {}).get("buy_orders") or {}
    sell_orders = (state or {}).get("sell_orders") or {}
    return {
        "title": title,
        "prices": list(prices),
        "levels": list((state or {}).get("levels") or []),
        "held": set((state or {}).get("held_buy_rungs") or ()),
        "buy_rungs": {int(v.get("rung", -1)) for v in buy_orders.values()},
        "sell_rungs": {int(v.get("rung", -1)) for v in sell_orders.values()},
        "mark_price": mark_price,
    }
