"""Captain Falcon — full-speed rush, grab when close, knee in the air."""
import melee


class Bot:
    def __init__(self):
        self._frame = 0

    def act(self, gamestate: melee.GameState, player_port: int) -> dict | None:
        self._frame += 1
        me = gamestate.players.get(player_port)
        if me is None:
            return None

        target = _nearest(gamestate, player_port)
        if target is None:
            return None

        dx = target.position.x - me.position.x
        dist = abs(dx)
        airborne = me.action not in (
            melee.Action.STANDING, melee.Action.WALK_SLOW,
            melee.Action.WALK_MIDDLE, melee.Action.WALK_FAST,
            melee.Action.DASHING, melee.Action.RUNNING,
        )

        stick_x = 1.0 if dx > 0 else 0.0

        # Grab when very close on ground
        grab = dist < 8 and not airborne
        # Knee (forward-air) when airborne and in range
        knee = airborne and dist < 20
        # Jump to start aerial approach every ~45 frames
        jump = not airborne and dist > 15 and self._frame % 45 == 0

        return {
            "stick_x": stick_x,
            "stick_y": 0.5,
            "buttons": {
                "BUTTON_A": grab or knee,
                "BUTTON_B": False,
                "BUTTON_X": jump,
                "BUTTON_Y": False,
                "BUTTON_L": False,
                "BUTTON_R": False,
                "BUTTON_Z": grab,
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
