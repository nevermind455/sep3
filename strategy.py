import math


def decide(start_price, current_price):
    if start_price is None or current_price is None:
        return None
    # Equality is not evidence for either outcome.  The old >= comparison
    # turned an unchanged opening/current price into an UP vote, which is
    # especially dangerous when phase 2 is allowed to sample at the boundary.
    # Keep this guard deliberately narrow: non-numeric/non-finite inputs retain
    # the comparison behavior that callers had before this fix.
    if start_price == current_price:
        try:
            if math.isfinite(start_price) and math.isfinite(current_price):
                return None
        except TypeError:
            pass
    return "UP" if current_price >= start_price else "DOWN"


def final_decision(price_side, book_side, chainlink_side):
    if price_side and book_side and price_side == book_side:
        return price_side
    if chainlink_side and chainlink_side in (price_side, book_side):
        return chainlink_side
    return price_side or book_side or chainlink_side


def minority_decision(price_side, book_side, chainlink_side):
    """Follow the DISSENTING signal when the three disagree.

    Two votes for UP and one for DOWN selects DOWN. Unanimity selects the
    agreed side, because there is no dissent to follow. Neutral or missing
    signals do not vote: a signal that abstained has not disagreed with
    anything, and treating silence as dissent would invent a direction.

    A 1-1 split has no minority, so it returns None and the caller abstains
    rather than breaking the tie arbitrarily.
    """
    votes = [s for s in (price_side, book_side, chainlink_side)
             if s in ("UP", "DOWN")]
    if not votes:
        return None
    if len(set(votes)) == 1:
        # Unanimous (including a lone vote): nobody dissented, so the agreed
        # side stands. Without this the majority test below inverts a clean
        # 3-0 into its opposite.
        return votes[0]
    up = votes.count("UP")
    down = len(votes) - up
    if up == down:
        return None
    return "DOWN" if up > down else "UP"
