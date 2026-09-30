"""
Physical robot command layer — Unitree Go2 (робопес) / G1 чи R1 EDU (гуманоїд).

Заліза ще немає. RobotController визначає словник фізичних дій, які може
попросити голосовий асистент; StubBackend — єдина реалізація, що працює вже
зараз (логує намір, завжди "успішна"), щоб tool-шар в assistant.py можна було
підключити й перевірити ще до появи робота.

Коли з'явиться робот: дописати Go2Backend (unitree_sdk2_python SportClient)
або HumanoidBackend (LocoClient + G1ArmActionClient) і перемкнути
ROBOT_BACKEND у .env. Вище цього модуля (assistant.py, tool-визначення)
нічого міняти не треба.
"""
import logging

logger = logging.getLogger(__name__)

# Спільний для собаки й гуманоїда набір дій — обидва SDK (SportClient у Go2,
# LocoClient+ArmActionClient у G1/R1) мають прямі відповідники для кожної.
ROBOT_ACTIONS: tuple[str, ...] = (
    "move_forward", "move_backward", "turn_left", "turn_right",
    "stop", "sit", "stand_up", "stand_down", "greet",
)


class StubBackend:
    """Заглушка без заліза — лише логує, яку дію виконав би робот."""

    def _log(self, action: str) -> None:
        logger.info("[robot:stub] would %s", action)

    def move_forward(self) -> None: self._log("move_forward")
    def move_backward(self) -> None: self._log("move_backward")
    def turn_left(self) -> None: self._log("turn_left")
    def turn_right(self) -> None: self._log("turn_right")
    def stop(self) -> None: self._log("stop")
    def sit(self) -> None: self._log("sit")
    def stand_up(self) -> None: self._log("stand_up")
    def stand_down(self) -> None: self._log("stand_down")
    def greet(self) -> None: self._log("greet")


class Go2Backend:
    """Unitree Go2 (робопес) через unitree_sdk2_python SportClient.

    Ще не підключено — немає заліза й пакета unitree_sdk2_python. Коли пес
    приїде: ChannelFactoryInitialize(network_interface), SportClient().Init(),
    і кожен метод нижче стає прямим викликом (Move/StandUp/StandDown/Sit/
    Hello/StopMove). Довідка:
    https://support.unitree.com/home/en/developer/sports_services
    """

    def __init__(self, network_interface: str = "") -> None:
        raise NotImplementedError(
            "Go2Backend ще не реалізовано — потрібен підключений робот і unitree_sdk2_python."
        )


class HumanoidBackend:
    """Unitree G1 або R1 EDU (гуманоїд) через LocoClient + G1ArmActionClient.

    Ще не підключено — немає заліза, і який саме гуманоїд купимо (G1 чи R1)
    поки не вирішено. Обидва на одному SDK2 API (LocoClient — рух,
    ArmActionClient — жести на кшталт greet), тож цей один backend має
    підійти для будь-якого з двох без змін вище.
    """

    def __init__(self, network_interface: str = "") -> None:
        raise NotImplementedError(
            "HumanoidBackend ще не реалізовано — потрібен підключений робот і unitree_sdk2_python."
        )


def create_robot_controller(backend: str, network_interface: str = ""):
    """backend: "stub" (дефолт, без заліза) | "go2" | "humanoid"."""
    if backend == "go2":
        return Go2Backend(network_interface)
    if backend == "humanoid":
        return HumanoidBackend(network_interface)
    return StubBackend()
