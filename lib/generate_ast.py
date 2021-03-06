#!/usr/bin/env python3
#
# Reverse : Generate an indented asm code (pseudo-C) with colored syntax.
# Copyright (C) 2015    Joel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.    If not, see <http://www.gnu.org/licenses/>.
#

import sys
import time

from lib.ast import (Ast_Branch, Ast_Goto, Ast_Loop,
        Ast_IfGoto, Ast_Ifelse, Ast_AndIf)
from lib.utils import BRANCH_NEXT, BRANCH_NEXT_JUMP, debug__
from lib.exceptions import ExcIfelse


class Endpoint():
    def __init__(self, ast, unseen, l_start):
        self.ast = [ast]
        self.unseen = unseen
        self.loop_start = [l_start]

    def rendezvous(self, ast, prev, l_start):
        self.ast.append(ast)
        self.loop_start.append(l_start)
        if prev in self.unseen:
            self.unseen.remove(prev)


# For one ast: search the next address. This is used for a gotos
# if they are at the end of a branch, we have to go up parents ast
# and get the next address.
def find_next_addr(ast):
    par = ast.parent
    if ast is None:
        return -1
    i = ast.idx_in_parent
    if i + 1 < len(par.nodes) and isinstance(par.nodes[i + 1], list):
        return par.nodes[i + 1][0].address
    return find_next_addr(par)


def remove_all_unnecessary_goto(ast):
    if isinstance(ast, Ast_Branch):
        if len(ast.nodes) > 0 and isinstance(ast.nodes[-1], Ast_Goto):
            if not ast.nodes[-1].dont_remove:
                nxt = find_next_addr(ast)
                if ast.nodes[-1].addr_jump == nxt:
                    del ast.nodes[-1]
        else:
            for n in ast.nodes:
                if not isinstance(n, list):
                    remove_all_unnecessary_goto(n)

    elif isinstance(ast, Ast_Ifelse):
        remove_all_unnecessary_goto(ast.br_next_jump)
        remove_all_unnecessary_goto(ast.br_next)

    elif isinstance(ast, Ast_Loop):
        remove_all_unnecessary_goto(ast.branch)


def search_endpoint(ctx, stack, ast, entry, l_set, l_prev_loop, l_start):
    waiting = {}
    visited = set()

    stack = []
    for n in ctx.gph.link_out[entry]:
        stack.append((entry, n))

    while 1:
        while stack:
            prev, ad = stack.pop(-1)

            # Don't go outside the current loop : we want to search
            # an if-endpoint.
            if l_prev_loop != -1 and ad not in l_set:
                continue

            # If "ad" is in last_loop_node we are sure that the path
            # will loop. So don't keep it if it's a subloop.

            if ad in ctx.gph.last_loop_node and \
                    (l_prev_loop, l_start) not in ctx.gph.last_loop_node[ad]:
                continue

            # If endpoint == loop : maybe the endpoint is at the end of the loop
            # If we have multiple link in, and if it's not a new loop, wait

            lkin = ctx.gph.link_in[ad]

            if ad == l_start or len(lkin) > 1:
                unseen = get_unseen_links_in(ad, l_set, l_prev_loop, l_start)
                if len(unseen) > 1:
                    if ad in waiting:
                        if prev in waiting[ad]:
                            waiting[ad].remove(prev)
                    else:
                        unseen.remove(prev)
                        waiting[ad] = unseen
                    continue

            if ad in visited:
                continue

            visited.add(ad)

            if ad in ctx.gph.link_out:
                for n in ctx.gph.link_out[ad]:
                    stack.append((ad, n))

        if not waiting:
            return -1

        if len(waiting) == 1:
            ad = next(iter(waiting.keys()))
            return ad

        stack = []
        w = {}

        restart = True
        while restart:
            restart = False

            for ad in waiting:
                if len(waiting[ad]) > 0:
                    w[ad] = set(waiting[ad])
                    continue
                visited.add(ad)

                if ad in ctx.gph.link_out:
                    for n in ctx.gph.link_out[ad]:
                        stack.append((ad, n))

            waiting = w

            # If the stack is still empty but if we have still some waiting
            # nodes, search if paths are really possible. If not, delete
            # a dependence.

            if not stack and waiting:
                for ad in set(waiting):
                    for i in set(waiting[ad]):
                        if not ctx.gph.path_exists(entry, i):
                            waiting[ad].remove(i)
                            if len(waiting[ad]) > 0:
                                restart = True
                            else:
                                del waiting[ad]

                if len(waiting) == 1:
                    ad = next(iter(waiting.keys()))
                    return ad

        if not stack:
            return -1


def get_unseen_links_in(ad, l_set, l_prev_loop, l_start, check_sub_loop=True):
    unseen = set(ctx.gph.link_in[ad])

    # Remove external jumps to the current node if it's an "equivalent loop"
    if (l_prev_loop, l_start) in ctx.gph.equiv and \
        (l_prev_loop, ad) in ctx.gph.equiv[(l_prev_loop, l_start)]:
        for prev in ctx.gph.link_in[ad]:
            if prev not in l_set and prev in unseen:
                unseen.remove(prev)

    # Is it the beginning of a loop ?
    # Remove internal links to the beginning of the loop
    if check_sub_loop:
        if (l_start, ad) in ctx.gph.loops_all:
            sub_loop = ctx.gph.loops_all[(l_start, ad)]
            for prev in ctx.gph.link_in[ad]:
                if prev in sub_loop and prev in unseen:
                    unseen.remove(prev)

    if l_set is None:
        return unseen

    # Remove external jumps which are outside the current loop
    for prev in ctx.gph.link_in[ad]:
        if prev not in l_set and prev in unseen:
            unseen.remove(prev)

    return unseen


def remove_unnecessary_goto(ast, ad):
    if len(ast.nodes) > 1:
        if isinstance(ast.nodes[-1], Ast_Goto) and \
                ast.nodes[-1].addr_jump == ad:
            ast.nodes.pop(-1)


def manage_endpoint(ctx, waiting, ast, prev, ad, l_set, l_prev_loop,
                    l_start, ad_is_visited):
    if ad not in ctx.gph.link_in or len(ctx.gph.link_in[ad]) <= 1:
        return ast

    # If ad_is_visited is False it means this is a prevision for a future
    # visit on this node. Here prev has no sense.

    if not ad_is_visited:
        if ad not in waiting:
            unseen = get_unseen_links_in(ad, l_set, l_prev_loop, l_start)
            waiting[ad] = Endpoint(ast, unseen, l_start)
        return None

    if ad in waiting:
        waiting[ad].rendezvous(ast, prev, l_start)

        if len(waiting[ad].unseen) != 0:
            return None

        # Get the ast which has the smallest level

        min_level_idx = -1
        list_ast = waiting[ad].ast
        list_loop_start = waiting[ad].loop_start

        for i, a in enumerate(list_ast):
            if (list_loop_start[i], ad) in ctx.gph.false_loops:
                continue
            if min_level_idx == -1 or a.level < list_ast[min_level_idx].level:
                min_level_idx = i

        if min_level_idx == -1:
            print("errorD: this is a bug, please report")
            sys.exit(1)

        ast = list_ast[min_level_idx]

        for i, a in enumerate(list_ast):
            if i != min_level_idx and len(a.nodes) == 0:
                a.add(Ast_Goto(ad))

        waiting[ad].ast.clear()
        del waiting[ad]

        return ast

    unseen = get_unseen_links_in(ad, l_set, l_prev_loop, l_start)

    if len(unseen) > 1:
        unseen.remove(prev)
        waiting[ad] = Endpoint(ast, unseen, l_start)
        return None

    return ast


def generate_ast(ctx__):
    global ctx
    ctx = ctx__

    start = time.clock()

    ast = Ast_Branch()
    ast.parent = None
    stack = [(ast, [], -1, ctx.entry_addr, -1)]
    visited = set()
    waiting = {}

    ast_head = ast

    while stack:
        ast, loops_stack, prev, curr, else_addr = stack.pop(-1)

        # Check if we enter in a false loop (see gotoinloop*)
        if loops_stack:
            _, _, l_start = loops_stack[-1]
        else:
            l_start = ctx.entry_addr

        if (l_start, curr) in ctx.gph.false_loops:
            continue

        # Check if we have already an other equivalent loop in waiting.
        if (l_start, curr) in ctx.gph.equiv:
            eq = ctx.gph.equiv[(l_start, curr)]
            dont_enter = False
            for ad in waiting:
                for i in waiting[ad].loop_start:
                    if (i, ad) in eq:
                        dont_enter = True
                        break
                if dont_enter:
                    break
            if dont_enter:
                # Restart main loop
                continue

        blk = ctx.gph.nodes[curr]

        # Exit the current loop
        while loops_stack:
            l_ast, l_prev_loop, l_start = loops_stack[-1]
            l_set = ctx.gph.loops_all[(l_prev_loop, l_start)]
            if curr not in l_set:
                loops_stack.pop(-1)
                ast = l_ast.parent
            else:
                break

        if not loops_stack:
            l_prev_loop = -1
            l_start = ctx.entry_addr
            l_set = None

        level = ast.level

        # Add a goto for more readability
        if len(ast.nodes) > 0:
            last_node = ast.nodes[-1]
            if isinstance(last_node, (Ast_AndIf, Ast_Ifelse)):
                if curr != last_node.expected_next_addr:
                    a = Ast_Goto(last_node.expected_next_addr)
                    a.dont_remove = True
                    ast.add(a)

        if curr not in visited:
            # Check if we need to stop and wait on a node
            a = manage_endpoint(ctx, waiting, ast, prev, curr, l_set,
                                l_prev_loop, l_start, True)
            if a is None:
                continue
            ast = a
            remove_unnecessary_goto(ast, curr)

            # Check if we enter in a new loop
            is_new_loop = True
            if (l_start, curr) not in ctx.gph.loops_all:
                is_new_loop = False
            else:
                # Check if if it's not equivalent as the current loop
                if loops_stack:
                    l_ast, l_prev_loop, l_start = loops_stack[-1]
                    if (l_prev_loop, curr) in ctx.gph.equiv and \
                        (l_prev_loop, l_start) in ctx.gph.equiv[(l_prev_loop, curr)]:
                        is_new_loop = False

            if is_new_loop:
                level += 1
                a = Ast_Loop()
                a.level = level
                a.parent = ast
                a.branch.parent = ast
                a.branch.level = level
                a.branch.idx_in_parent = len(ast.nodes)
                ast.add(a)
                ast = a.branch
                loops_stack.append((a, l_start, curr))
                else_addr = -1
                l_ast = a
                l_set = ctx.gph.loops_all[(l_start, curr)]
                l_prev_loop = l_start
                l_start = curr
                if (l_prev_loop, l_start) in ctx.gph.infinite_loop:
                    a.is_infinite = True
            # Here curr may has changed

        if curr in visited:
            if curr == l_start:
                continue
            if not isinstance(ast.nodes[-1], list):
                continue
            prev_inst = ast.nodes[-1][0]
            if not ctx.libarch.utils.is_uncond_jump(prev_inst):
                ast.add(Ast_Goto(curr))
            continue

        visited.add(curr)

        # Return instruction
        if curr not in ctx.gph.link_out:
            ast.add(blk)
            continue

        nxt = ctx.gph.link_out[curr]

        if len(nxt) == 2:
            # We are on a conditional jump

            prefetch = blk[1] if len(blk) == 2 else None

            if loops_stack:
                goto_set = False

                c1 = nxt[BRANCH_NEXT] not in l_set
                c2 = nxt[BRANCH_NEXT_JUMP] not in l_set

                if c1 and c2:
                    raise ExcIfelse(curr)

                if c1:
                    exit_loop = nxt[BRANCH_NEXT]
                    nxt_node_in_loop = nxt[BRANCH_NEXT_JUMP]
                    cond_id = ctx.libarch.utils.invert_cond(blk[0])
                    goto_set = True

                if c2:
                    exit_loop = nxt[BRANCH_NEXT_JUMP]
                    nxt_node_in_loop = nxt[BRANCH_NEXT]
                    cond_id = ctx.libarch.utils.get_cond(blk[0])
                    goto_set = True

                # goto to exit a loop
                if goto_set:
                    stack.append((ast.parent, list(loops_stack), curr,
                                  exit_loop, else_addr))
                    stack.append((ast, list(loops_stack), curr,
                                  nxt_node_in_loop, else_addr))
                    a = Ast_IfGoto(blk[0], cond_id, exit_loop, prefetch)
                    a.parent = ast
                    a.level = level
                    ast.add(a)
                    continue

            # and-if
            if ctx.print_andif:
                if else_addr == nxt[BRANCH_NEXT_JUMP]:
                    cond_id = ctx.libarch.utils.invert_cond(blk[0])
                    a = Ast_AndIf(blk[0], cond_id, nxt[BRANCH_NEXT], prefetch)
                    a.parent = ast
                    ast.add(a)
                    stack.append((ast, list(loops_stack), curr,
                                  nxt[BRANCH_NEXT], else_addr))
                    stack.append((ast, list(loops_stack), curr,
                                  nxt[BRANCH_NEXT_JUMP], else_addr))
                    continue

                # and-if
                if else_addr == nxt[BRANCH_NEXT]:
                    cond_id = ctx.libarch.utils.get_cond(blk[0])
                    a = Ast_AndIf(blk[0], cond_id, nxt[BRANCH_NEXT_JUMP], prefetch)
                    a.parent = ast
                    ast.add(a)
                    stack.append((ast, list(loops_stack), curr,
                                  nxt[BRANCH_NEXT], else_addr))
                    stack.append((ast, list(loops_stack), curr,
                                  nxt[BRANCH_NEXT_JUMP], else_addr))
                    continue

            # if-else

            endpoint = search_endpoint(ctx, stack, ast, curr,
                                       l_set, l_prev_loop, l_start)

            ast_if = Ast_Branch()
            ast_if.parent = ast
            ast_if.level = level + 1
            ast_if.idx_in_parent = len(ast.nodes)

            ast_else = Ast_Branch()
            ast_else.parent = ast
            ast_else.level = level + 1
            ast_else.idx_in_parent = len(ast.nodes)

            else_addr = nxt[BRANCH_NEXT_JUMP]

            if endpoint != -1:
                if (l_start, endpoint) not in ctx.gph.false_loops:
                    manage_endpoint(ctx, waiting, ast, -1, endpoint, l_set,
                                    l_prev_loop, l_start, False)
                else:
                    endpoint = -1

            stack.append((ast_if, list(loops_stack), curr,
                          nxt[BRANCH_NEXT], else_addr))

            if endpoint == -1:
                a = Ast_Ifelse(blk[0], ast_else, ast_if, else_addr, prefetch)
                stack.append((ast, list(loops_stack), curr,
                              nxt[BRANCH_NEXT_JUMP], else_addr))
            elif endpoint == else_addr:
                # Branch ast_else will be empty
                a = Ast_Ifelse(blk[0], ast_else, ast_if, endpoint, prefetch)
                stack.append((ast, list(loops_stack), curr,
                              nxt[BRANCH_NEXT_JUMP], else_addr))
            else:
                a = Ast_Ifelse(blk[0], ast_else, ast_if, endpoint, prefetch)
                stack.append((ast_else, list(loops_stack), curr,
                              nxt[BRANCH_NEXT_JUMP], else_addr))

            a.parent = ast
            a.level = level + 1
            ast.add(a)

        else:
            ast.add(blk)
            stack.append((ast, loops_stack, curr,
                          nxt[BRANCH_NEXT], else_addr))


    ast = ast_head

    remove_all_unnecessary_goto(ast)

    elapsed = time.clock()
    elapsed = elapsed - start
    debug__("Ast generated in %fs" % elapsed)

    # Process ast

    start = time.clock()

    for func in ctx.libarch.registered:
        func(ctx, ast)

    elapsed = time.clock()
    elapsed = elapsed - start
    debug__("Functions for processing ast in %fs" % elapsed)

    if ctx.color:
        ctx.libarch.process_ast.assign_colors(ctx, ast)

    return ast
