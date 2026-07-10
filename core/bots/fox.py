"""Fox — pure aggression. Chase the nearest opponent and up-smash."""
import melee


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

        stick_x = 0.5
        if dx > 2:
            stick_x = 1.0
        elif dx < -2:
            stick_x = 0.0

        # Up-smash when in range: hold stick up + A
        up_smash = dist < 10
        stick_y = 1.0 if up_smash else 0.5

        return {
            "stick_x": stick_x,
            "stick_y": stick_y,
            "buttons": {
                "BUTTON_A": dist < 12,
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
