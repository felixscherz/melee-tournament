"""Falco — short-hop laser spam from mid-range, rush in when target is stunned."""
import melee

LASER_RANGE = (25, 55)


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
        airborne = me.position.y > 1.0

        stick_x = 1.0 if dx > 0 else 0.0

        in_laser_range = LASER_RANGE[0] <= dist <= LASER_RANGE[1]
        # Short-hop every 30 frames when in laser range
        jump = in_laser_range and not airborne and self._frame % 30 == 0
        # Fire laser (B) while airborne and in range
        laser = airborne and in_laser_range
        # Close range: run in and shine (down + B)
        shine = dist < 12
        # Approach when too far
        approach = dist > LASER_RANGE[1]

        return {
            "stick_x": stick_x if (approach or shine) else 0.5,
            "stick_y": 0.0 if shine else 0.5,
            "buttons": {
                "BUTTON_A": shine,
                "BUTTON_B": laser or shine,
                "BUTTON_X": jump,
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
