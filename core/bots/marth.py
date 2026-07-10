"""Marth — spacing game. Sit at tipper range and forward-smash."""
import melee

TIPPER_MIN = 18
TIPPER_MAX = 28


class Bot:
    def act(self, gamestate: melee.GameState, player_port: int) -> dict | None:
        me = gamestate.players.get(player_port)
        if me is None:
            return None

        target = _nearest(gamestate, player_port)
        if target is None:
            return None

        dx = target.position.x - me.position.x
        dist = abs(dx)
        facing_target = (dx > 0 and me.facing) or (dx < 0 and not me.facing)

        # Walk into tipper range
        stick_x = 0.5
        if dist > TIPPER_MAX:
            stick_x = 1.0 if dx > 0 else 0.0
        elif dist < TIPPER_MIN:
            stick_x = 0.0 if dx > 0 else 1.0  # back off

        # Forward-smash at tipper range when facing the target
        f_smash = TIPPER_MIN <= dist <= TIPPER_MAX and facing_target

        return {
            "stick_x": 1.0 if (f_smash and dx > 0) else (0.0 if (f_smash and dx < 0) else stick_x),
            "stick_y": 0.5,
            "buttons": {
                "BUTTON_A": f_smash,
                "BUTTON_B": False,
                "BUTTON_X": False,
                "BUTTON_Y": False,
                "BUTTON_L": False,
                "BUTTON_R": False,
                "BUTTON_Z": False,
            },
        }


def _nearest(gamestate: melee.GameState, port: int):
    me = gamestate.players.get(port)
    if me is None:
        return None
    closest, dist = None, float("inf")
    for p, player in gamestate.players.items():
        if p == port:
            continue
        d = abs(player.position.x - me.position.x)
        if d < dist:
            dist = d
            closest = player
    return closest
