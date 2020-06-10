# inspired by https://eshapard.github.io/
#
# KNOWN BUGS / TODO:
# Undo messes up scheduling. Monkey patching from cordone below:
#   https://gist.github.com/cordone/38317befecfbda6e12d4f04df4555065
#   Maybe there's a way to request a better hook for this...
#   Needs testing.
# Long tooltips fall off the screen, so I've both monkey patched tooltip() here
#   and also submitted a PR to anki to let tooltip() take x and y offsets
#   remove that patch once that PR goes live in next version
#   Note: "Set Font Size" and similar addons might cause offscreen tooltips.
# Tweak initial starting assumptions (ease 2500 and 350 backoff, min/max)
# Implement additional config options
# Move schedulers and tooltip to separate files for readability

from __future__ import annotations

import math
import random
import time
from heapq import heappush

from anki import version
from anki.hooks import addHook
from aqt import mw
from aqt.utils import tooltip

anki21 = version.startswith("2.1.")

config = mw.addonManager.getConfig(__name__)

target_ratio = config.get('target_ratio', 0.85)
moving_average_weight = config.get('moving_average_weight', 0.2)
show_stats = config.get('show_stats', True)


class EaseAlgorithm(object):

    def __init__(self):
        self.factor = 0
        self.last_tooltip_msg = None

    @staticmethod
    def calculate_moving_average(l):
        result = l[0]
        for i in l[1:]:
            result = (result * (1 - moving_average_weight))
            result += i * moving_average_weight
        return result

    def find_success_rate(self, card_id):
        review_list = mw.col.db.list(("select ease from revlog where cid = ?"),
                                     card_id)
        if not review_list:
            return 0

        success_list = [int(i > 1) for i in review_list]
        success_rate = self.calculate_moving_average(success_list)
        return success_rate

    def find_average_ease(self, card_id):
        average_ease = 0
        ease_list = mw.col.db.list("select (1000*ivl/lastIvl) from revlog"
                                   " where cid = ? and lastIvl > 0 and "
                                   "ivl > 0",
                                   card_id)
        if not ease_list or ease_list is None:
            average_ease = 2500
        else:
            average_ease = self.calculate_moving_average(ease_list)
        return average_ease

    def calculate_ease(self, card_id):
        success_rate = self.find_success_rate(card_id)
        # Ebbinghaus formula
        if success_rate > 0.99:
            success_rate = 0.99  # ln(1) = 0; avoid divide by zero error
        if success_rate < 0.01:
            success_rate = 0.01
        delta_ratio = math.log(target_ratio) / math.log(success_rate)
        average_ease = self.find_average_ease(card_id)
        suggested_factor = int(round(average_ease * delta_ratio))

        # anchor this to 2500 starting out
        number_of_reviews = len(mw.col.db.list(("select ease from revlog where"
                                                " cid = ?"), card_id))
        ease_cap = min(7000, (2500 + 350 * number_of_reviews))
        if suggested_factor > ease_cap:
            suggested_factor = ease_cap
        ease_floor = max(100, (2500 - 350 * number_of_reviews))
        if suggested_factor < ease_floor:
            suggested_factor = ease_floor

        return suggested_factor

    def adjust_ease(self):
        card_id = mw.reviewer.card.id
        calculated_ease = self.calculate_ease(card_id)
        self.factor = calculated_ease

        # tooltip messaging
        if show_stats:
            review_list = mw.col.db.list(("select ease from revlog where "
                                          "cid = ?"), card_id)

            success_rate = self.find_success_rate(card_id)

            msg = ("cardID: {}<br/> sRate: {} curFactor: {} sugFactor: {}<br> "
                   "rlist: {}<br>".format(card_id, round(success_rate, 4),
                                          round(mw.reviewer.card.factor),
                                          calculated_ease, review_list))
            if self.last_tooltip_msg is not None:
                new_msg = (self.last_tooltip_msg
                           + "<br><br>  *   *   *   <br><br>"
                           + msg)
                tooltip_args = {'msg': new_msg, 'period': 9000, 'x_offset': 12,
                                'y_offset': 250}
                tooltip(**tooltip_args)
            else:
                tooltip_args = {'msg': msg, 'period': 9000, 'x_offset': 12,
                                'y_offset': 140}
                tooltip(**tooltip_args)
            self.last_tooltip_msg = msg


# OVERRIDING SCHEDULER V2 FUNCTIONS EASE CALCULATIONS

def rescheduleLapse_V2(self, card):
    conf = self._lapseConf(card)

    card.lapses += 1
    # card.factor = max(1300, card.factor-200)
    card.factor = alg.factor
    # showInfo("New Card Ease:%s" % card.factor)
    suspended = self._checkLeech(card, conf) and card.queue == -1

    if conf['delays'] and not suspended:
        card.type = 3
        delay = self._moveToFirstStep(card, conf)
    else:
        # no relearning steps
        self._updateRevIvlOnFail(card, conf)
        self._rescheduleAsRev(card, conf, early=False)
        # need to reset the queue after rescheduling
        if suspended:
            card.queue = -1
        delay = 0

    return delay


def rescheduleRev_V2(self, card, ease, early):
    # update interval
    card.lastIvl = card.ivl
    if early:
        self._updateEarlyRevIvl(card, ease)
    else:
        self._updateRevIvl(card, ease)

    # then the rest
    # card.factor = max(1300, card.factor+[-150, 0, 150][ease-2])
    card.factor = alg.factor
    # showInfo("New Card Ease: %s" % card.factor)
    card.due = self.today + card.ivl

    # card leaves filtered deck
    self._removeFromFiltered(card)


# SCHEDULER V1 FUNCTIONS

def rescheduleLapse_V1(self, card):
    conf = self._lapseConf(card)
    card.lastIvl = card.ivl
    if self._resched(card):
        card.lapses += 1
        card.ivl = self._nextLapseIvl(card, conf)
        # card.factor = max(1300, card.factor-200)
        card.factor = alg.factor
        # showInfo("New Card Ease: %s" % card.factor)
        card.due = self.today + card.ivl
        # if it's a filtered deck, update odue as well
        if card.odid:
            card.odue = card.due
    # if suspended as a leech, nothing to do
    delay = 0
    if self._checkLeech(card, conf) and card.queue == -1:
        return delay
    # if no relearning steps, nothing to do
    if not conf['delays']:
        return delay
    # record rev due date for later
    if not card.odue:
        card.odue = card.due
    delay = self._delayForGrade(conf, 0)
    card.due = int(delay + time.time())
    card.left = self._startingLeft(card)
    # queue 1
    if card.due < self.dayCutoff:
        self.lrnCount += card.left // 1000
        card.queue = 1
        heappush(self._lrnQueue, (card.due, card.id))
    else:
        # day learn queue
        ahead = ((card.due - self.dayCutoff) // 86400) + 1
        card.due = self.today + ahead
        card.queue = 3
    return delay


def rescheduleRev_V1(self, card, ease):
    # update interval
    card.lastIvl = card.ivl
    if self._resched(card):
        self._updateRevIvl(card, ease)
        # card.factor = max(1300, card.factor+[-150, 0, 150][ease-2])
        card.factor = alg.factor
        # showInfo("New Card Ease: %s" % card.factor)
        card.due = self.today + card.ivl
    else:
        card.due = card.odue
    if card.odid:
        card.did = card.odid
        card.odid = 0
        card.odue = 0


from anki.sched import Scheduler as V1

V1._rescheduleLapse = rescheduleLapse_V1
V1._rescheduleRev = rescheduleRev_V1

if anki21:
    from anki.schedv2 import Scheduler as V2

    V2._rescheduleLapse = rescheduleLapse_V2
    V2._rescheduleRev = rescheduleRev_V2


alg = EaseAlgorithm()
addHook('showQuestion', alg.adjust_ease)

# Patching tooltip() to allow x and y offsets
# TODO - clean up imports
# NOTE - the change in arguments to tooltip() was accepted by the Anki devs,
# so everything below this can be removed as soon as that goes live
import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Optional, Union

import anki
import aqt
from anki.lang import _
from anki.rsbackend import TR  # pylint: disable=unused-import
from anki.utils import (invalidFilename, isMac, isWin, noBundledLibs,
                        versionWithBuild)
from aqt.qt import *
from aqt.theme import theme_manager

if TYPE_CHECKING:
    from anki.rsbackend import TRValue

_activeTooltips = []
_tooltipTimer: Optional[QTimer] = None
_tooltipLabel: Optional[QLabel] = None


def tooltip(msg, period=3000, parent=None, x_offset=0, y_offset=100):
    global _tooltipTimer, _tooltipLabel

    class CustomLabel(QLabel):
        silentlyClose = True

        def mousePressEvent(self, evt):
            evt.accept()
            self.hide()

    closeTooltip()
    aw = parent or aqt.mw.app.activeWindow() or aqt.mw

    # prevent tooltip values outside of main window
    if y_offset > aw.size().height():
        y_offset = aw.size().height()
    if y_offset < 100:
        y_offset = 100
    if x_offset > mw.size().width() - 390:
        x_offset = mw.size().width() - 390
    if x_offset < 0:
        x_offset = 0

    lab = CustomLabel(
        """\
<table cellpadding=10>
<tr>
<td>%s</td>
</tr>
</table>"""
        % msg,
        aw,
    )
    lab.setFrameStyle(QFrame.Panel)
    lab.setLineWidth(2)
    lab.setWindowFlags(Qt.ToolTip)
    if not theme_manager.night_mode:
        p = QPalette()
        p.setColor(QPalette.Window, QColor("#feffc4"))
        p.setColor(QPalette.WindowText, QColor("#000000"))
        lab.setPalette(p)
    lab.move(aw.mapToGlobal(QPoint(0+x_offset, aw.height() - y_offset)))
    lab.show()
    _tooltipTimer = aqt.mw.progress.timer(
        period, closeTooltip, False, requiresCollection=False
    )
    _tooltipLabel = lab


def closeTooltip():
    global _tooltipLabel, _tooltipTimer
    if _tooltipLabel:
        try:
            _tooltipLabel.deleteLater()
        except:
            # already deleted as parent window closed
            pass
        _tooltipLabel = None
    if _tooltipTimer:
        _tooltipTimer.stop()
        _tooltipTimer = None
