"""
Bot template — copy this file, rename it, and implement the `act` method.

Upload the finished .py file via the dashboard. Your Bot class will be
hot-reloaded into the game loop without restarting Dolphin.
"""

import melee


class Bot:
    """
    Required interface: implement `act(gamestate, player_port) -> dict | None`

    Return None to release all inputs for that frame.
    """

    def __init__(self):
        # Put any one-time setup here (e.g. loading a trained model)
        pass

    def act(self, gamestate: melee.GameState, player_port: int) -> dict | None:
        """
        Called once per frame (~60fps) while the match is running.

        Parameters
        ----------
        gamestate : melee.GameState
            Full snapshot of the current game state.
        player_port : int
            Which controller port this bot is driving (1 or 2).

        Returns
        -------
        dict with keys:
            stick_x   : float, 0.0 (left) – 1.0 (right), 0.5 = neutral
            stick_y   : float, 0.0 (down) – 1.0 (up),    0.5 = neutral
            buttons   : dict[str, bool]  e.g. {"BUTTON_A": True, "BUTTON_B": False}
        or None to release all inputs.
        """
        my = gamestate.players.get(player_port)
        opponent_port = 2 if player_port == 1 else 1
        opp = gamestate.players.get(opponent_port)

        if my is None or opp is None:
            return None

        # --- Example: chase the opponent and press A ---
        stick_x = 0.5
        if opp.position.x > my.position.x:
            stick_x = 1.0  # walk right
        elif opp.position.x < my.position.x:
            stick_x = 0.0  # walk left

        close_enough = abs(opp.position.x - my.position.x) < 15

        return {
            "stick_x": stick_x,
            "stick_y": 0.5,
            "buttons": {
                "BUTTON_A": close_enough,
                "BUTTON_B": False,
                "BUTTON_X": False,
                "BUTTON_Y": False,
                "BUTTON_L": False,
                "BUTTON_R": False,
                "BUTTON_Z": False,
            },
        }
