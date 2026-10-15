from pathlib import Path

from uwanja.schedule import FINISHED, STARTED, Schedule
from uwanja.session import reason_of


def test_gauntlet_order_matches_fastchess():
    schedule = Schedule("subj", ["opp-a", "opp-b"], rounds=2)
    games = [(e.number, e.white, e.black, e.round) for e in schedule.entries]
    assert games == [(1, "subj", "opp-a", 1), (2, "opp-a", "subj", 1), (3, "subj", "opp-b", 2),
                     (4, "opp-b", "subj", 2), (5, "subj", "opp-a", 3), (6, "opp-a", "subj", 3),
                     (7, "subj", "opp-b", 4), (8, "opp-b", "subj", 4)]
    assert schedule.progress() == {"total": 8, "played": 0, "playing": 0}


def test_fastchess_lines():
    started = STARTED.match("Started game 3 of 8 (subj vs opp-b)")
    assert started.groups() == ("3", "8", "subj", "opp-b")
    finished = FINISHED.match("Finished game 2 (opp-a vs subj): 1-0 {White mates}")
    assert finished.groups() == ("2", "opp-a", "subj", "1-0", "White mates")
    assert FINISHED.match("Finished game 9 (a vs b): 1/2-1/2 {Draw by 3-fold repetition}")[5] == "Draw by 3-fold repetition"


def test_bind_takes_the_first_unmatched_game_with_these_colours():
    schedule = Schedule("subj", ["opp-a"], rounds=2)
    first = schedule.bind("subj", "opp-a")
    second = schedule.bind("subj", "opp-a")          # round 2's, the first is taken
    other = schedule.bind("opp-a", "subj")
    assert (first.number, second.number, other.number) == (1, 3, 2)
    extra = schedule.bind("subj", "opp-a")           # more games than scheduled: appended, not lost
    assert extra.number == 5 and len(schedule.entries) == 5


def test_started_corrects_the_colours_and_finished_names_the_fault():
    schedule = Schedule("subj", ["opp-a"], rounds=1)
    assert schedule.started(1, "opp-a", "subj")      # fastchess' word over the guess
    entry = schedule.get(1)
    assert (entry.white, entry.state) == ("opp-a", "playing")
    schedule.finished(entry, "1-0", "White disconnects")
    assert entry.fault == {"kind": "crashes", "side": "opponent"}
    schedule.finished(schedule.get(2), "0-1", "White loses on time")
    assert schedule.get(2).fault == {"kind": "timelosses", "side": "opponent"}  # game 2: opp-a has white
    assert schedule.progress() == {"total": 2, "played": 2, "playing": 0}


def test_reason_from_a_final_comment():
    assert reason_of("+M1/1 0.000s, White mates") == "White mates"
    assert reason_of("0.00/16 0.131s, Draw by adjudication") == "Draw by adjudication"
    assert reason_of("+0.31/18 0.452s") is None
    assert reason_of("+0.31/18 0.452s, n=2200412, sd=0") is None
