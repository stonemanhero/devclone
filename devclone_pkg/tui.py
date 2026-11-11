"""Interactive terminal selector (curses arrow-key menu + Rich numbered fallback)."""

from __future__ import annotations

import curses
import sys
from typing import List, Optional

from rich.console import Console
from rich.prompt import Prompt

from .discovery import ProjectInfo

console = Console()


def select_project_interactive(
    projects: List[ProjectInfo],
    title: str = "Select a project (↑/↓ to move, / to filter, Enter to select, q to quit)",
    only_available: bool = False,
) -> Optional[ProjectInfo]:
    """
    Open an interactive arrow-key selector in the terminal using curses.
    Falls back to a numbered prompt if stdin/stdout is not a TTY.
    """
    candidates = [p for p in projects if (not p.installed_locally if only_available else True)]
    if not candidates:
        return None

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _select_fallback(candidates, title)

    try:
        return curses.wrapper(_curses_selector, candidates, title)
    except Exception:
        return _select_fallback(candidates, title)


def _curses_selector(stdscr: "curses._CursesWindow", candidates: List[ProjectInfo], title: str) -> Optional[ProjectInfo]:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_CYAN)

    filter_str = ""
    in_filter_mode = False
    idx = 0
    scroll = 0

    while True:
        filtered = [
            p for p in candidates
            if not filter_str
            or filter_str.lower() in p.name.lower()
            or filter_str.lower() in p.stack.lower()
        ]
        if idx >= len(filtered):
            idx = max(0, len(filtered) - 1)

        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # Header
        header = f" {title} "
        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        stdscr.addstr(0, 0, header[: max_x - 1])
        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

        filter_bar = (
            f" Filter (/): {filter_str}{'_' if in_filter_mode else ''}   "
            f"[Showing {len(filtered)}/{len(candidates)}] "
        )
        stdscr.addstr(1, 0, filter_bar[: max_x - 1])

        col_hdr = f"   {'PROJECT':<26} {'STACK':<22} {'FILES':<10} {'DB / VOLS':<12} {'DESKTOP':<15} {'LAPTOP':<18}"
        stdscr.attron(curses.A_UNDERLINE | curses.A_BOLD)
        stdscr.addstr(2, 0, col_hdr[: max_x - 1])
        stdscr.attroff(curses.A_UNDERLINE | curses.A_BOLD)

        list_height = max(1, max_y - 5)
        if idx < scroll:
            scroll = idx
        elif idx >= scroll + list_height:
            scroll = idx - list_height + 1

        for row_i, p in enumerate(filtered[scroll : scroll + list_height]):
            actual_i = scroll + row_i
            y = 3 + row_i
            if y >= max_y - 1:
                break
            status_tag = "[✓] Installed" if p.installed_locally else "[ ] Available"
            marker = " ▸ " if actual_i == idx else "   "
            line = (
                f"{marker}{p.name:<26.26} {p.stack:<22.22} "
                f"{p.size_human:<10.10} {p.total_volume_size_human:<12.12} "
                f"{p.desktop_state_summary:<15.15} {status_tag:<18.18}"
            )
            if actual_i == idx:
                stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
                stdscr.addstr(y, 0, line[: max_x - 1].ljust(min(len(line) + 2, max_x - 1)))
                stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
            else:
                if p.installed_locally:
                    stdscr.attron(curses.color_pair(2))
                stdscr.addstr(y, 0, line[: max_x - 1])
                if p.installed_locally:
                    stdscr.attroff(curses.color_pair(2))

        footer = " [↑/↓] Navigate   [Enter] Select   [/] Search Filter   [Esc/q] Cancel "
        if max_y > 4:
            stdscr.attron(curses.color_pair(3))
            stdscr.addstr(max_y - 1, 0, footer[: max_x - 1])
            stdscr.attroff(curses.color_pair(3))

        stdscr.refresh()
        ch = stdscr.getch()

        if in_filter_mode:
            if ch in (10, 13, 27):  # Enter or Esc exits filter input mode
                in_filter_mode = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                filter_str = filter_str[:-1]
                idx = 0
            elif 32 <= ch <= 126:
                filter_str += chr(ch)
                idx = 0
            continue

        if ch in (ord("q"), ord("Q"), 27):
            return None
        elif ch == ord("/"):
            in_filter_mode = True
        elif ch in (curses.KEY_UP, ord("k")):
            if filtered:
                idx = (idx - 1) % len(filtered)
        elif ch in (curses.KEY_DOWN, ord("j")):
            if filtered:
                idx = (idx + 1) % len(filtered)
        elif ch == curses.KEY_PPAGE:
            idx = max(0, idx - list_height)
        elif ch == curses.KEY_NPAGE:
            idx = min(max(0, len(filtered) - 1), idx + list_height)
        elif ch in (10, 13, curses.KEY_ENTER):
            if filtered:
                return filtered[idx]


def _select_fallback(candidates: List[ProjectInfo], title: str) -> Optional[ProjectInfo]:
    console.print(f"\n[bold cyan]{title}[/bold cyan]")
    for i, p in enumerate(candidates, 1):
        status = "[green][✓] Installed[/green]" if p.installed_locally else "[yellow][ ] Available[/yellow]"
        console.print(
            f"  [bold]{i:2d}.[/bold] {p.name:<26} [dim]{p.stack:<22}[/dim] "
            f"Files: {p.size_human:<9} Vols: {p.total_volume_size_human:<9} {status}"
        )
    ans = Prompt.ask("\nEnter project number or name (or 'q' to cancel)", default="q").strip()
    if not ans or ans.lower() == "q":
        return None
    if ans.isdigit():
        num = int(ans)
        if 1 <= num <= len(candidates):
            return candidates[num - 1]
    for p in candidates:
        if p.name.lower() == ans.lower():
            return p
    return None
