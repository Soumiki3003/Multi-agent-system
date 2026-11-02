# angr_ctf Levels 00–17 — Complete Solving Guide
### (Symbolic Execution / Fuzzing — CS 492/592 Malware Reverse Engineering, angr_ctf series)

---

## 0. Before you start

**Every level ships as a scaffold** (`scaffoldXX.py`) with `???` placeholders and a `??_angr_LEVELNAME` binary.
The binaries are **randomized per student** — addresses, register choices, and target strings in this
guide are the *illustrative example values shown in lecture*. Your binary will have different numbers.
The code structure and method are identical; you must re-derive the concrete values from **your own binary**.

**How to find your own values, every time:**

```bash
# Disassemble everything, find main() and any function of interest
objdump -d ./binary_name

# Find where a specific string is used / referenced
objdump -d ./binary_name | grep -A2 -B2 "call.*<function_name>"

# Find raw ASCII/strings baked into the binary (target passwords, filenames)
strings -a ./binary_name

# Find a symbol's address precisely
readelf -s ./binary_name | grep function_name

# Confirm anything at runtime
gdb ./binary_name
(gdb) disas main
(gdb) x/s 0x0804a050      # dump a string at an address
(gdb) info functions
```

**Universal script skeleton** every level below follows:

```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]
    project = angr.Project(path_to_binary)
    # ... level-specific state setup ...
    simulation = project.factory.simgr(initial_state)
    simulation.explore(find=is_successful, avoid=should_abort)
    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

def is_successful(state):
    stdout_output = state.posix.dumps(sys.stdout.fileno())
    return b'Good Job.' in stdout_output

def should_abort(state):
    stdout_output = state.posix.dumps(sys.stdout.fileno())
    return b'Try again.' in stdout_output

if __name__ == '__main__':
    main(sys.argv)
```

**Core vocabulary you need for every level:**

| Term | Meaning |
|---|---|
| Bitvector (`BVS`/`BVV`) | A value of fixed bit-width. `BVS` = symbolic (can be many values), `BVV` = concrete (one fixed value) |
| Concrete | Bitvector with exactly 1 possible value |
| Symbolic | Bitvector with >1 possible value, constrained by the path taken so far |
| Unconstrained | Bitvector that can be *anything* within its bit-width (no constraints yet) |
| Unsatisfiable | Bitvector whose constraints contradict each other (no valid value exists) |
| `state.solver.eval(bv)` | Ask z3 (angr's constraint solver) for **one** concrete value satisfying the current constraints |
| `simulation.explore(find=X, avoid=Y)` | Step every active state; keep states matching `find`, kill/stash states matching `avoid` |

Run any solved script with: `python3 solve.py ./binary_name`

---

## Level 00 — `00_angr_find`

**New concept:** the absolute basics — load a binary, symbolically execute from `main()`, and search for one address you want the program to reach.

**Binary behavior:** input is passed through an obfuscated `complex_function()` specifically so *manual* reverse engineering is painful, but symbolic execution solves it in well under a minute regardless.

**What you must find in your binary:**
- The address of the instruction that prints the "Good Job." / success message (look for the `call` to `printf`/`puts` right after the success string is loaded — find that string's cross-reference in `objdump -d` or IDA/Ghidra/gdb).

**Solution script:**
```python
import angr
import sys

def main(argv):
    path_to_binary = argv[1]                       # ./00_angr_find
    project = angr.Project(path_to_binary)

    initial_state = project.factory.entry_state()  # start at main()
    simulation = project.factory.simgr(initial_state)

    print_good_address = 0x804867a                 # <-- FIND THIS in your binary
    simulation.explore(find=print_good_address)

    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `project.factory.entry_state()` starts symbolic execution at the program's real entry (`main`), and angr **automatically** injects a symbol for stdin input — you don't set up anything manually here.
- You only need the **address of success**, not a full pass/fail condition function yet — that's `simgr.explore(find=<int address>)`.
- `simulation.found` is a list; grab index `[0]` and decode its stdin dump — that IS your password.

---

## Level 01 — `01_angr_avoid`

**New concept:** using `find` *and* `avoid` together so angr terminates dead-end paths early instead of wasting time exploring them.

**Binary behavior:** same shape as level 00, but now there's a second address representing a "wrong path" / failure branch you want angr to actively prune.

**What you must find in your binary:**
- The success address (as in level 00).
- The address of the failure branch ("Try again." print, or a trap/detour path) to pass as `avoid`.

**Solution script:**
```python
import angr
import sys

def main(argv):
    path_to_binary = argv[1]                        # ./01_angr_avoid
    project = angr.Project(path_to_binary)

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    print_good_address = 0x80430a                    # <-- FIND THIS (success)
    print_avoid_address = 0x9aa442                    # <-- FIND THIS (failure / trap)

    simulation.explore(find=print_good_address, avoid=print_avoid_address)

    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `find=`/`avoid=` both accept a single address, a list of addresses, **or** a predicate function — level 01 uses plain addresses (simplest form); level 02 upgrades to predicate functions.
- Any state that hits `avoid` gets moved to `simulation.avoid`/terminated and is never explored further — this is a pure performance/pruning optimization on top of level 00's technique, not a new algorithm.

---

## Level 02 — `02_angr_find_condition`

**New concept:** instead of hunting for one hardcoded address, define success/failure as **conditions on program output** (a Python predicate function) — this is far more robust when the exact success address is hard to pin down or the binary has multiple success paths.

**Binary behavior:** prints either `"Good Job."` or `"Try again."` to stdout depending on the input.

**What you must find in your binary:** nothing address-specific — just confirm the exact success/failure strings printed (usually `"Good Job."` / `"Try again."`, but confirm with `strings`).

**Solution script:**
```python
import angr
import sys

def main(argv):
    path_to_binary = argv[1]                        # ./02_angr_find_condition
    project = angr.Project(path_to_binary)

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        stdout_output = state.posix.dumps(sys.stdout.fileno())    # bytes, not str!
        return b'Good Job.' in stdout_output

    def should_abort(state):
        stdout_output = state.posix.dumps(sys.stdout.fileno())
        return b'Try again.' in stdout_output

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints / gotcha (this trips up almost everyone):**
- angr's I/O is UTF-8 **bytes**, so `state.posix.dumps(...)` returns a `bytes` object (`type() == <class 'bytes'>`).
- `'Good Job'.encode() in stdout_out` (substring containment test, correct) is completely different from `stdout_out == 'Good Job'.encode()` (exact equality test, almost always wrong because of trailing newlines/other output) — use **`in`**, not `==`, unless you've confirmed the exact full byte string.
- This `is_successful`/`should_abort` pair is reused **verbatim** in nearly every later level — memorize it.

---

## Level 03 — `03_angr_symbolic_registers`

**New concept:** manual symbol injection. Instead of letting angr symbolically execute a complicated input routine (slow, blows up), start execution **after** the input routine returns and manually write symbolic bitvectors into the registers that routine would have populated.

**Binary behavior:** three numbers are read as the password via a formatted `scanf` inside `get_user_input()`. `scanf` with format strings is expensive/unreliable to run symbolically, but `get_user_input()` conveniently returns its 3 parsed values in registers (e.g. `eax`, `edx`, `esi`) before they're written to memory.

**What you must find in your binary:**
- The address **immediately after** the call to `get_user_input()` returns (before its results are moved out of registers) — e.g. `0x080488d1`.
- Which registers hold which value on return (read the `mov %eax,-0xc(%ebp)` / `mov %edx,...` / `mov %esi,...` instructions right after the call).
- The bit-width of each value (a 32-bit int = 32 bits; an 8-char string = 64 bits, etc).

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                         # ./03_angr_symbolic_registers
    project = angr.Project(path_to_binary)

    start_address = 0x080488d1                        # <-- FIND THIS (just after get_user_input returns)
    initial_state = project.factory.blank_state(addr=start_address)

    password0 = claripy.BVS('password0', 32)          # <-- confirm bit-width per register
    password1 = claripy.BVS('password1', 32)
    password2 = claripy.BVS('password2', 32)

    initial_state.regs.eax = password0                 # <-- match registers to YOUR disassembly
    initial_state.regs.edx = password1
    initial_state.regs.esi = password2

    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        solution0 = solution_state.solver.eval(password0)
        solution1 = solution_state.solver.eval(password1)
        solution2 = solution_state.solver.eval(password2)
        print(f'{solution0} {solution1} {solution2}')
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `project.factory.blank_state(addr=...)` (not `entry_state()`) — you're starting mid-program, so there's no automatic stdin symbol injection; you inject the symbols yourself.
- `state.regs.<name> = <bitvector>` writes either a concrete (`state.regs.eax = 0x0`) or symbolic (`state.regs.eax = my_bitvector`) value directly to a register.
- Because you skipped `scanf` entirely, `solution_state.posix.dumps(stdin)` won't give you the answer anymore — you must `.solver.eval()` each bitvector individually and format them yourself (space-separated ints, matching however the real program expects them typed in).

---

## Level 04 — `04_angr_symbolic_stack`

**New concept:** manual symbol injection onto the **stack**, which requires you to recreate the function's stack frame by hand before execution begins mid-function.

**Binary behavior:** `handle_user()` reads two passwords directly onto the stack (e.g. `password1` at `ebp-0x10`, `password0` at `ebp-0xc`), then runs a complex obfuscation function on each before checking them.

**What you must find in your binary:**
- The address right after both values are read onto the stack, before the complex check runs (e.g. `0x80486ae`).
- Each variable's **stack offset from `ebp`** (via `mov ...,-0xc(%ebp)`-style instructions) and its **size in bytes**.

**The algorithm (memorize this — it recurs anywhere you inject onto the stack):**
1. Set `ebp = esp` (you're recreating the frame from scratch).
2. Compute how much **padding** separates `esp` from the stack slot **closest to `ebp`** — `padding_bytes = offset_of_nearest_variable - size_of_that_variable`. (Slide example: if the variable at `ebp-0xc` is 4 bytes, padding = 8; if it's 8 bytes, padding = 4 — i.e. `padding = 0xc - size_of_that_variable`.)
3. Subtract that padding from `esp`.
4. **Push in order from the variable closest to `ebp` to the one farthest from `ebp`** (push moves `esp` further down/away from `ebp` each time).

```
#            /-------- The stack --------\
# ebp ->     |          padding          |
#            |---------------------------|
#                        . . .
#            |---------------------------|
# ebp - 0x0b |   password0, second byte  |   <-- pushed FIRST (closer to ebp)
# ebp - 0x0c |   password0, first byte   |
#            |---------------------------|
#                        . . .
#            |---------------------------|
# ebp - 0x0f |   password1, second byte  |   <-- pushed SECOND (farther from ebp)
# ebp - 0x10 |   password1, first byte   |
#            |---------------------------|
```

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./04_angr_symbolic_stack
    project = angr.Project(path_to_binary)

    start_address = 0x80486ae                           # <-- FIND THIS (just after both reads, before checks)
    initial_state = project.factory.blank_state(addr=start_address)

    password0 = claripy.BVS('password0', 32)            # <-- confirm size per your disassembly
    password1 = claripy.BVS('password1', 32)

    initial_state.regs.ebp = initial_state.regs.esp     # recreate the frame
    padding_length_in_bytes = 8                          # <-- 0xc - size_of(password0); confirm from YOUR binary
    initial_state.regs.esp -= padding_length_in_bytes

    initial_state.stack_push(password0)                 # closest to ebp -> pushed first
    initial_state.stack_push(password1)                 # farthest from ebp -> pushed second

    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        solution0 = solution_state.solver.eval(password0)
        solution1 = solution_state.solver.eval(password1)
        print(f'{solution0} {solution1}')
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `state.stack_push(bitvector)` mimics a real `push` — it decrements `esp` by the value's byte-width, then stores the value there.
- Get the push order backwards and your bitvectors land at the wrong offsets — the check will always fail even though your logic is "close." If solving hangs or returns garbage, re-verify offsets with `objdump -d` first.

---

## Level 05 — `05_angr_symbolic_memory`

**New concept:** manual symbol injection into **global/static memory** (`.bss`/`.data`), simpler than the stack because there's no frame or padding to reconstruct — just write directly to the known fixed address.

**Binary behavior:** password values are read via `scanf` directly into global memory locations rather than the stack.

**What you must find in your binary:**
- The address right after `scanf` returns, before the complex check function runs.
- The exact **global memory address** each password is stored at (the literal address passed as `scanf`'s destination argument — visible in the `push $0x0804....` right before the `call scanf@plt`).
- Each value's size in bits.

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./05_angr_symbolic_memory
    project = angr.Project(path_to_binary)

    start_address = 0x8048671                           # <-- FIND THIS (just after scanf returns)
    initial_state = project.factory.blank_state(addr=start_address)

    password0 = claripy.BVS('password0', 64)            # <-- confirm bit-width
    password0_address = 0x0804a054                       # <-- FIND THIS (scanf's destination arg)
    initial_state.memory.store(password0_address, password0)

    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        solution0 = solution_state.solver.eval(password0, cast_to=bytes).decode()
        print(solution0)
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `state.memory.store(address, bitvector)` writes symbolic (or concrete) data directly to any memory address — global memory needs no setup beyond knowing the address.
- `eval()` returns a plain **integer** by default. If the password is meant to be typed as a **string**, pass `cast_to=bytes` and `.decode()` the result — forgetting this gives you a huge meaningless integer instead of the actual password text.

---

## Level 06 — `06_angr_symbolic_dynamic_memory`

**New concept:** injecting symbols behind a **heap pointer** whose target address isn't known ahead of time (it comes from `malloc`, so it changes run to run) — solved by redirecting the pointer to a fake address you invent yourself.

**Binary behavior:** two buffers are `malloc`'d; pointers to them are stored at fixed addresses (e.g. `obj.buffer1` at `0xa2def74`, `obj.buffer2` at `0xa2def7c`), and passwords are `scanf`'d into the heap memory those pointers reference.

**Do the actual heap addresses matter?** No — and they aren't knowable statically anyway. **Strategy:** pick two arbitrary, unused fake addresses (e.g. `0x444444`/`0x444454`), overwrite the *pointer* locations to point at your fake addresses, then drop your symbolic bitvectors at those fake addresses.

**What you must find in your binary:**
- Start address (just after both `scanf` calls, before the checking function).
- The **addresses of the pointer variables** themselves (`obj.buffer1`, `obj.buffer2` — fixed/static, even though what they point *to* is dynamic).

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./06_angr_symbolic_dynamic_memory
    project = angr.Project(path_to_binary)

    start_address = 0x80486af                            # <-- FIND THIS
    initial_state = project.factory.blank_state(addr=start_address)

    password0 = claripy.BVS('password0', 64)
    password1 = claripy.BVS('password1', 64)

    fake_heap_address0 = 0x444444                         # any address nothing else uses
    fake_heap_address1 = 0x444454

    pointer_to_malloc_memory_address0 = 0xa2def74          # <-- FIND THIS (obj.buffer1's own address)
    pointer_to_malloc_memory_address1 = 0xa2def7c          # <-- FIND THIS (obj.buffer2's own address)

    initial_state.memory.store(
        pointer_to_malloc_memory_address0,
        fake_heap_address0,
        endness=project.arch.memory_endness)
    initial_state.memory.store(
        pointer_to_malloc_memory_address1,
        fake_heap_address1,
        endness=project.arch.memory_endness)

    initial_state.memory.store(fake_heap_address0, password0)
    initial_state.memory.store(fake_heap_address1, password1)

    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        solution0 = solution_state.solver.eval(password0, cast_to=bytes).decode()
        solution1 = solution_state.solver.eval(password1, cast_to=bytes).decode()
        print(f'{solution0} {solution1}')
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- Set the **endianness** explicitly (`endness=project.arch.memory_endness`) when storing a raw pointer value — get this wrong and the pointer will reference garbage.
- Two `memory.store()` calls per password: one to redirect the pointer, one to plant the symbolic data at the fake destination. Missing either one breaks the chain.

---

## Level 07 — `07_angr_symbolic_file`

**New concept:** injecting a symbolic **file** into angr's simulated filesystem, for when input comes from disk instead of stdin/registers/memory.

**Binary behavior:** password is written to a randomly-named file on disk, then the program `fopen()`s that file and `fread()`s a fixed number of bytes back into a buffer before the complex check runs.

**What you must find in your binary:**
- Start address, just before the file is opened.
- The exact **filename** the program opens (check the string passed to `fopen` — `strings`/disassembly).
- The exact **byte count** passed to `fread` (this defines your bitvector's size in bits = bytes × 8).

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./07_angr_symbolic_file
    project = angr.Project(path_to_binary)

    start_address = 0x80488d3                            # <-- FIND THIS (just before fopen)
    initial_state = project.factory.blank_state(addr=start_address)

    filename = 'WXYZABCD.txt'                             # <-- FIND THIS (the exact string fopen() is called with)
    symbolic_file_size_bytes = 64                          # <-- FIND THIS (fread's byte-count arg)

    password = claripy.BVS('password', symbolic_file_size_bytes * 8)

    password_file = angr.storage.SimFile(filename, content=password)
    initial_state.fs.insert(filename, password_file)

    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        solution = solution_state.solver.eval(password, cast_to=bytes).decode()
        print(solution)
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- The simulated filesystem (`state.fs`) is separate from program memory — `SimFile` treats file contents like a little memory region of their own, addressed from `0x0` at the file's start, not tied to any real program address.
- If your filename string doesn't **exactly** match what the binary passes to `fopen`, angr's simulated `fopen` will fail to find the "file" and the symbolic data never gets read in.

---

## Level 08 — `08_angr_constraints`

**New concept:** when a checking function is *too complex to symbolically execute* (branches explosively — e.g. checking 16 characters one at a time creates 2¹⁶ branches), skip past it entirely and manually **add a constraint by hand** describing what the check function would have required.

**Binary behavior:** input goes through `complex_function()` (obfuscation), then gets compared byte-by-byte against a static string inside `check_equals_XXXXXXXX(...)` — a function that would blow up symbolic execution if actually run. `check_equals_XXXXXXXX(to_check, 16)` is logically equivalent to `if (*to_check == "TARGETSTRING16CH")`.

**What you must find in your binary:**
- Start address just after `scanf` (skip straight past reading input into a buffer).
- The **buffer address** the password is stored at.
- The address **right before** the call into `check_equals_XXXXXXXX` (stop exploration there — never let angr actually step into the expensive function).
- The exact target string `check_equals_XXXXXXXX` compares against (read it from the disassembly/binary — it's a static string, e.g. `"BWYRUBQCMVSBRGFU"`).

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./08_angr_constraints
    project = angr.Project(path_to_binary)

    start_address = 0x8048651                            # <-- FIND THIS (just after scanf)
    initial_state = project.factory.blank_state(addr=start_address)

    password = claripy.BVS('password', 16 * 8)            # <-- confirm length (16 bytes here)
    password_address = 0x804a050                          # <-- FIND THIS (password buffer address)
    initial_state.memory.store(password_address, password)

    simulation = project.factory.simgr(initial_state)

    address_to_check_constraint = 0x8048671                # <-- FIND THIS (just before check_equals_XXXX is called)
    simulation.explore(find=address_to_check_constraint)

    if simulation.found:
        solution_state = simulation.found[0]

        constrained_parameter_address = 0x804a050            # <-- same as password_address
        constrained_parameter_size_bytes = 16
        constrained_parameter_bitvector = solution_state.memory.load(
            constrained_parameter_address,
            constrained_parameter_size_bytes
        )

        constrained_parameter_desired_value = 'BWYRUBQCMVSBRGFU'.encode()   # <-- FIND THIS (target string)

        solution_state.add_constraints(
            constrained_parameter_bitvector == constrained_parameter_desired_value
        )

        solution = solution_state.solver.eval(password, cast_to=bytes).decode()
        print(solution)
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `simulation.explore(find=<address>)` here has **no `avoid`** — you're not solving pass/fail yet, you're just walking up to the doorway of the expensive function and stopping.
- The constraint you add (`bitvector == target_string`) is applied to `solution_state` **after** exploration — this is the trick: you never ran the expensive function, you just declared "if it *had* run, it would have needed this input" and let z3 back-propagate that all the way to your original `password` symbol via the constraint graph already accumulated by `complex_function()`.

---

## Level 09 — `09_angr_hooks`

**New concept:** function **hooks** — a more general/reusable alternative to level 08's approach. Instead of manually building a constraint post-hoc, you replace the entire problematic function call with your own Python implementation that runs instead of the real (explosive) machine code.

**Binary behavior:** same shape as level 08 (`check_equals_XXXX` blows up symbolic execution), but this time you eliminate the problem by hooking the **call site** and substituting your own equivalent logic that runs natively in Python and sets the return value (`eax`) correctly.

**What you must find in your binary:**
- The address of the `call` instruction that invokes `check_equals_XXXX`.
- The byte-length of that `call` instruction (typically 5 bytes for a relative `call` on x86 — confirm in disassembly).
- The buffer address/length being checked, and the target string.

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./09_angr_hooks
    project = angr.Project(path_to_binary)

    check_equals_called_address = 0x80486ca              # <-- FIND THIS (address of the `call` instruction)
    instruction_to_skip_length = 5                        # <-- FIND THIS (byte length of that call instruction)

    @project.hook(check_equals_called_address, length=instruction_to_skip_length)
    def skip_check_equals_(state):
        user_input_buffer_address = 0x0804a044             # <-- FIND THIS
        user_input_buffer_length = 16                       # <-- FIND THIS

        user_input_string = state.memory.load(
            user_input_buffer_address,
            user_input_buffer_length
        )

        check_against_string = 'ZZZZZZZZZZZZZZZZ'.encode()   # <-- FIND THIS (target string)

        state.regs.eax = claripy.If(
            user_input_string == check_against_string,
            claripy.BVV(1, 32),
            claripy.BVV(0, 32)
        )

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `@project.hook(address, length=N)` replaces `N` bytes of real machine code starting at `address` with your Python function — angr skips straight over the original instructions.
- Set `length=0` if you want your hook to run **in addition to** (not instead of) the original code — not needed here, but useful to know.
- Return values from a hooked function must be written to the register/location the real calling convention expects (here: `eax`, 32-bit cdecl return) — this is the one manual bit hooks don't do for you (SimProcedures, next level, add sugar on top of this).

---

## Level 10 — `10_angr_simprocedures`

**New concept:** `SimProcedure` — syntactic sugar over hooks, built for replacing an entire **function** (by symbol name, wherever it's called from) rather than one specific call site. Necessary once a function is called *many* times (hooking every individual call site by address becomes impractical).

**Why level 09's technique doesn't scale here:** in this binary, `check_equals_XXXXXXXXXXXXXXXX` isn't called once — check with:
```bash
objdump -d 10_angr_simprocedures | egrep check_equals_XXXXXXXXXXXXXXXX | egrep call | wc -l
# -> 256
```
Manually hooking 256 call sites (or adding 256 individual constraints) is impractical. Instead, hook the **function itself, once, by name** — `SimProcedure` runs automatically every time *any* code calls that symbol.

**What you must find in your binary:**
- The exact exported/local symbol name of the check function (from the `objdump`/`nm` output, e.g. `check_equals_WQNDNKKWAWOLXBAC`).
- Its parameter calling convention (pointer to buffer, length) and the target string.

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./10_angr_simprocedures
    project = angr.Project(path_to_binary)

    class ReplacementCheckEquals(angr.SimProcedure):
        def run(self, to_check, length):
            to_check_str = self.state.memory.load(to_check, length)

            equals_str = 'WQNDNKKWAWOLXBAC'.encode()      # <-- FIND THIS (target string, from binary)

            return claripy.If(
                to_check_str == equals_str,
                claripy.BVV(1, 32),
                claripy.BVV(0, 32)
            )

    check_equals_symbol = 'check_equals_WQNDNKKWAWOLXBAC'   # <-- FIND THIS (exact mangled function name)
    project.hook_symbol(check_equals_symbol, ReplacementCheckEquals())

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `SimProcedure.run(self, arg0, arg1, ...)` gets called with the function's *actual* parameters already parsed for you (angr's calling-convention-aware — no manual `esp+4`/`esp+8` reads like a raw hook needs).
- `return <value>` from `run()` automatically gets placed in the correct return-value location (`eax` on x86) — no manual `state.regs.eax = ...` needed, unlike level 09's plain hook.
- `project.hook_symbol(name, SimProcedureInstance())` hooks by **symbol name**, not address — every call site pointing at that symbol gets replaced, regardless of how many there are.

---

## Level 11 — `11_angr_sim_scanf`

**New concept:** writing your own `SimProcedure` to replace **`scanf` itself**, needed when the built-in angr `scanf` SimProcedure can't handle your format string (angr's stock version only supports a single format specifier; this level's `scanf` uses multiple, e.g. `"%u %20s"`).

**Binary behavior:** `scanf` reads two values with different types/format specifiers in one call — angr's default handling of `scanf` bails out or mishandles this, so you replace it wholesale.

**What you must find in your binary:**
- The exact libc symbol name for `scanf` used by your binary (commonly `__isoc99_scanf` on modern glibc — check with `nm`/`objdump`).
- The two destination addresses (parameters after the format string) and appropriate bit-widths for each value being read.

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./11_angr_sim_scanf
    project = angr.Project(path_to_binary)

    class ReplacementScanf(angr.SimProcedure):
        def run(self, format_string, scanf_addr0, scanf_addr1):
            scanf0 = claripy.BVS('scanf0', 32)            # <-- confirm bit-width per format specifier
            scanf1 = claripy.BVS('scanf1', 64)

            self.state.memory.store(
                scanf_addr0, scanf0,
                endness=project.arch.memory_endness)
            self.state.memory.store(
                scanf_addr1, scanf1,
                endness=project.arch.memory_endness)

            self.state.globals['solution0'] = scanf0        # stash for later, since stdin dump won't have it
            self.state.globals['solution1'] = scanf1

    scanf_symbol = '__isoc99_scanf'                        # <-- FIND THIS (confirm exact symbol in your binary)
    project.hook_symbol(scanf_symbol, ReplacementScanf())

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        stored_solution0 = solution_state.globals['solution0']
        stored_solution1 = solution_state.globals['solution1']
        solution0 = solution_state.solver.eval(stored_solution0)
        solution1 = solution_state.solver.eval(stored_solution1, cast_to=bytes).decode()
        print(f'{solution0} {solution1}')
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `state.globals` is a scratch dict that persists on the state object across the rest of execution — necessary here because once you've replaced `scanf`, the "answer" no longer flows through the normal stdin buffer, so `posix.dumps(stdin)` won't reconstruct it for you like in earlier levels.
- Match each `claripy.BVS` bit-width to what its format specifier actually consumes (`%u` → 32-bit int, `%20s` → up to 20 bytes = 160 bits, etc.) — get this wrong and the constraint solver either can't satisfy the check or gives you a malformed answer.

---

## Level 12 — `12_angr_veritesting`

**New concept:** *Veritesting* — angr's automatic state-merging optimization that collapses many near-identical branching states back into one, taming state explosion **without** you writing any hooks/constraints/SimProcedures.

**Binary behavior:** a loop compares each of 32 input characters against `complex_function(...)`, generating a huge number of branches — structurally similar to level 08/09/10's problem, but here you're asked to solve it by flipping on an angr optimization flag instead of manually intervening.

**What you must find in your binary:** nothing new — reuse level 02's exact `is_successful`/`should_abort` predicate approach.

**Solution script:**
```python
import angr
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./12_angr_veritesting
    project = angr.Project(path_to_binary)

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state, veritesting=True)   # <-- the only change from level 02

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- First try running your **unmodified level-02 script** directly against this binary — it will technically work but may take several minutes; this demonstrates *why* veritesting matters before you're told to use it.
- The only functional change needed is passing `veritesting=True` into `project.factory.simgr(...)`. Everything else is identical to level 02.
- The merging heuristic itself is a black box for this course ("out of scope") — you don't need to understand its internals, just know when to flip it on: many similar branches converging on the same downstream logic (loops over independent characters, bytes, etc).

---

## Level 13 — `13_angr_static_binary`

**New concept:** handling **statically-linked** binaries. angr auto-replaces known libc calls (`scanf@plt`, `printf@plt`, etc.) with fast SimProcedures automatically — but only when it can recognize them via the dynamic linking table (PLT). Static binaries compile libc directly into `.text`, so angr has nothing to pattern-match against and will try to (slowly, or incorrectly) symbolically execute all of libc's internals.

**Binary behavior:** functionally identical to the first few levels' challenge logic, but compiled with `gcc -static`, so `printf`, `scanf`, `puts`, and the C runtime startup (`__libc_start_main`) are all baked directly into the binary's own code.

**What you must find in your binary:**
- The addresses where each of these functions begins in your binary (e.g. `printf` at `0x804fab0`) — find via `objdump -d` (look for the function prologues) or symbol table if not stripped (`nm ./binary | grep printf`).

**Solution script:**
```python
import angr
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./13_angr_static_binary
    project = angr.Project(path_to_binary)

    project.hook(0x804fab0, angr.SIM_PROCEDURES['libc']['printf']())      # <-- FIND THESE addresses
    project.hook(0x804fca0, angr.SIM_PROCEDURES['libc']['scanf']())
    project.hook(0x804f720, angr.SIM_PROCEDURES['libc']['puts']())
    project.hook(0x8048200, angr.SIM_PROCEDURES['glibc']['__libc_start_main']())

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        return b'Good Job.' in state.posix.dumps(sys.stdout.fileno())

    def should_abort(state):
        return b'Try again.' in state.posix.dumps(sys.stdout.fileno())

    simulation.explore(find=is_successful, avoid=should_abort)

    if simulation.found:
        solution_state = simulation.found[0]
        print(solution_state.posix.dumps(sys.stdin.fileno()).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `angr.SIM_PROCEDURES['libc']['<funcname>']()` gives you angr's **pre-written** SimProcedure for a standard libc function — you don't write your own logic, just tell angr where in *your* binary that function's real code starts so it can substitute the fast Python version.
- The full catalogue of pre-written SimProcedures ships in the angr source: `https://github.com/angr/angr/tree/master/angr/procedures/libc` (browse this if a function you need isn't obviously named).
- `__libc_start_main` must also be hooked for static binaries — otherwise angr tries to symbolically execute the entire C runtime startup sequence before it even reaches your `main()`.

---

## Level 14 — `14_angr_shared_library` (binary: `lib14_angr_shared_library.so`)

**New concept:** symbolically executing a function **inside a shared library** directly (rather than via a wrapping executable) — this means manually constructing a call into a specific function with manually-supplied parameters, since there's no `main()`/entry point to start from.

**Binary behavior:** the library exports a `validate(...)` function you must call directly, feeding it a buffer + length, and check its return value.

**What you must find in your binary:**
- `validate`'s offset within the `.so` (via `objdump -d lib14_angr_shared_library.so | tail -n 72 | head -5`, or search for its symbol — e.g. offset `0x6d7`).
- Its calling convention (parameters: pointer to buffer, length) and what return value signals success.

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./lib14_angr_shared_library.so
    base = 0x4000000                                     # arbitrary base address for a position-independent .so

    project = angr.Project(path_to_binary, load_options={
        'main_opts': {'base_addr': base}
    })

    buffer_pointer = claripy.BVV(0x3000000, 32)           # arbitrary unused address to store the buffer
    password = claripy.BVS('password', 64 * 8)             # <-- confirm expected buffer length in bytes

    validate_function_address = base + 0x6d7               # <-- FIND THIS (validate's offset within the .so)

    initial_state = project.factory.call_state(
        validate_function_address,
        buffer_pointer,
        len(password) // 8
    )
    initial_state.memory.store(buffer_pointer, password)

    simulation = project.factory.simgr(initial_state)

    check_constraint_address = base + 0x74a                # <-- FIND THIS (address validate() returns to)
    simulation.explore(find=check_constraint_address)

    if simulation.found:
        solution_state = simulation.found[0]
        solution_state.add_constraints(solution_state.regs.eax != 0)   # <-- confirm success condition (eax != 0, or == specific value)
        solution = solution_state.solver.eval(password, cast_to=bytes).decode()
        print(solution)
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- `project.factory.call_state(function_address, arg0, arg1, ...)` sets up a state as if you'd just called that function directly with those parameters — angr handles the calling-convention plumbing (pushing args, setting up a return address) for you.
- Position-independent `.so` files need an explicit `base_addr` at load time (`load_options={'main_opts': {'base_addr': base}}`) — all internal offsets (like `validate`'s) are then relative to that chosen base.
- There's no `stdin` here — the "input" is the buffer you manually populate with a symbolic bitvector, and the "output" is whatever `validate` returns in `eax`, so both your success condition and your final extraction method differ from stdin/stdout-based levels.

---

## Level 15 — `15_angr_arbitrary_read`

**New concept:** first of three **exploit-generation** levels. Instead of solving a "find the password" challenge, you're searching for a **buffer overflow** that lets you leak arbitrary memory — specifically, overwriting a pointer so that `puts()` prints something you weren't meant to see.

**Binary behavior:** a switch statement is set up so the program should only ever print "Try again." — but there's a vulnerable buffer overflow that lets you overwrite a `to_print` pointer, redirecting it to point at a `good_job` string elsewhere in memory, so `puts()` prints "Good Job." instead.

**High-level strategy:**
1. Replace `scanf` with a custom `SimProcedure` (as in level 11) so the input buffer + a length/key argument both become symbolic.
2. At **every** call to `puts`, check whether its string-pointer argument is symbolic.
3. If symbolic, add a *tentative* constraint: "can this pointer equal the address of `good_job`?" — check satisfiability before committing.
4. If satisfiable, commit the constraint and declare success.
5. Also constrain the raw input bytes to be printable, non-whitespace ASCII (so you can actually type the solution into a text prompt).

**What you must find in your binary:**
- The exact `scanf` format string used (e.g. `"%u %20s"`) and the two destination parameter positions.
- The address of the `good_job` string constant.
- `puts`'s PLT address (to detect "we just reached a call to puts").

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./15_angr_arbitrary_read
    project = angr.Project(path_to_binary)

    class ReplacementScanf(angr.SimProcedure):
        def run(self, format_string, key_addr, string_addr):
            scanf0 = claripy.BVS('scanf0', 32)             # the "key" (%u)
            scanf1 = claripy.BVS('scanf1', 20 * 8)           # the string (%20s)

            for char in scanf1.chop(bits=8):
                self.state.add_constraints(char >= 0x20, char <= 0x7e)   # printable, non-whitespace ASCII

            self.state.memory.store(key_addr, scanf0, endness=project.arch.memory_endness)
            self.state.memory.store(string_addr, scanf1, endness=project.arch.memory_endness)

            self.state.globals['solution0'] = scanf0
            self.state.globals['solution1'] = scanf1

    scanf_symbol = '__isoc99_scanf'                        # <-- FIND THIS
    project.hook_symbol(scanf_symbol, ReplacementScanf())

    def check_puts(state):
        puts_parameter = state.memory.load(
            state.regs.esp + 4, 4, endness=project.arch.memory_endness)

        if state.solver.symbolic(puts_parameter):
            good_job_string_address = 0x0804a050            # <-- FIND THIS (address of "Good Job."/good_job data)
            is_vulnerable_expression = (puts_parameter == good_job_string_address)

            if state.satisfiable(extra_constraints=(is_vulnerable_expression,)):
                state.add_constraints(is_vulnerable_expression)
                return True
        return False

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        puts_address = 0x08048460                          # <-- FIND THIS (puts@plt)
        if state.addr == puts_address:
            return check_puts(state)
        return False

    simulation.explore(find=is_successful)

    if simulation.found:
        solution_state = simulation.found[0]
        stored0 = solution_state.globals['solution0']
        stored1 = solution_state.globals['solution1']
        solution = f'{solution_state.solver.eval(stored0)} {solution_state.solver.eval(stored1, cast_to=bytes).decode()}'
        print(solution)
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- Load `puts`'s parameter from `esp + 4` (cdecl: first argument sits just above the return address on entry to the callee — do this the instant `state.addr == puts_address`, before the prologue touches `esp`).
- Always check `state.satisfiable(extra_constraints=(...))` **before** calling `state.add_constraints(...)` — committing an unsatisfiable constraint permanently corrupts that state; testing first lets you safely reject non-viable states and keep exploring others.
- **Gotcha called out explicitly for this level:** the pointer you overwrite and the *actual* `"Good Job."` string may live at **different addresses** — verify with `gdb`'s `x/s <address>` that the address you're constraining to truly contains the string you expect, don't assume the label name matches the print target.

---

## Level 16 — `16_angr_arbitrary_write`

**New concept:** the write-side analog of level 15 — instead of leaking memory via a redirected read, you redirect a **write** (`strncpy`'s destination) so attacker-controlled data gets copied into a security-relevant location (here, overwriting a stored password comparison buffer).

**Binary behavior:** `password_buffer` is preset to `"PASSWORD"` and compared against a fixed target string (e.g. `"DVTBOGZL"`) — normally always failing. But a switch statement's `strncpy` call can be hijacked: overflow a buffer to overwrite the `to_copy_to` pointer so it points at `password_buffer`, and `strncpy` will happily overwrite it with your chosen (attacker-controlled) source data instead.

**High-level strategy:**
1. Replace `scanf` with a SimProcedure (same pattern as level 11/15).
2. At every call to `strncpy`, check if **both** the destination pointer and the source buffer contents are symbolic.
3. If so, tentatively constrain: destination == `password_buffer`'s address, AND source contents == desired target string.
4. Check joint satisfiability before committing.

**What you must find in your binary:**
- `strncpy`'s three parameters' stack offsets on entry (`esp+4`=dest, `esp+8`=src, `esp+12`=len — standard cdecl for this signature, confirm against your disassembly).
- The address of `password_buffer`.
- The exact target comparison string.

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./16_angr_arbitrary_write
    project = angr.Project(path_to_binary)

    class ReplacementScanf(angr.SimProcedure):
        def run(self, format_string, addr0):
            scanf0 = claripy.BVS('scanf0', 32 * 8)         # <-- confirm buffer size
            self.state.memory.store(addr0, scanf0, endness=project.arch.memory_endness)
            self.state.globals['solution0'] = scanf0

    scanf_symbol = '__isoc99_scanf'                        # <-- FIND THIS
    project.hook_symbol(scanf_symbol, ReplacementScanf())

    def check_strncpy(state):
        strncpy_dest = state.memory.load(state.regs.esp + 4, 4, endness=project.arch.memory_endness)
        strncpy_src = state.memory.load(state.regs.esp + 8, 4, endness=project.arch.memory_endness)
        strncpy_len = state.memory.load(state.regs.esp + 12, 4, endness=project.arch.memory_endness)

        src_contents = state.memory.load(strncpy_src, 8)     # <-- confirm length ("DVTBOGZL" = 8 bytes)

        if state.solver.symbolic(strncpy_dest) and state.solver.symbolic(src_contents):
            password_string = 'DVTBOGZL'.encode()              # <-- FIND THIS (target comparison string)
            buffer_address = 0x0804a060                         # <-- FIND THIS (password_buffer's address)

            does_src_hold_password = (src_contents == password_string)
            does_dest_equal_buffer_address = (strncpy_dest == buffer_address)

            if state.satisfiable(extra_constraints=(does_src_hold_password, does_dest_equal_buffer_address)):
                state.add_constraints(does_src_hold_password, does_dest_equal_buffer_address)
                return True
        return False

    initial_state = project.factory.entry_state()
    simulation = project.factory.simgr(initial_state)

    def is_successful(state):
        strncpy_address = 0x08048490                        # <-- FIND THIS (strncpy@plt)
        if state.addr == strncpy_address:
            return check_strncpy(state)
        return False

    simulation.explore(find=is_successful)

    if simulation.found:
        solution_state = simulation.found[0]
        stored0 = solution_state.globals['solution0']
        print(solution_state.solver.eval(stored0, cast_to=bytes).decode())
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints / gotchas (explicitly called out for this level):**
- Common mistakes across both 15 and 16: forgetting to set `endness` on any raw pointer value, and forgetting to constrain input characters to printable/non-whitespace ASCII so the "solution" can actually be typed into a prompt.
- **Level 16 specific gotcha:** watch for the destination pointer being stored at an address whose *contents* point to a fake heap location where **both** the pointer *and* the memory it references are symbolic (a level-6-style indirection layered on top of this level's exploit) — don't assume a single dereference is enough; trace it in `gdb`/disassembly first.
- The `[-1:-64]`-style funky slicing you may see in some reference solutions is just claripy's bitvector array-slicing syntax for pulling out N bytes of a bitvector — a plain `state.memory.load(addr, length)` (as used above) achieves the same result more readably.

---

## Level 17 — `17_angr_arbitrary_jump`

**New concept:** the most involved exploit type — an **arbitrary jump**. This is a classic stack-based buffer overflow: overflow past a local buffer into the saved return address, so the function "returns" into `print_good()` instead of back to its real caller. The instruction pointer (`eip`) becomes **completely symbolic** at that point — user input directly controls where the CPU jumps next (conceptually: `mov user_input, eax` / `jmp eax`). angr calls this an **unconstrained state**, and by default it just throws such states away, since normally "eip could be anything" isn't useful to keep exploring. Here you deliberately keep and exploit it.

**Binary behavior:** a vulnerable `scanf`-based read overflows a stack buffer far enough to overwrite the saved return address with the address of `print_good`.

**Why `explore(find=...)` doesn't work here:** `find` is only ever evaluated against states with a concrete `eip` — it can't be checked against a state whose `eip` is symbolic. You must drive the stepping loop yourself and watch for unconstrained states directly.

**High-level strategy (this is what the official scaffold's structure encodes):**
1. Replace `scanf` with a custom `SimProcedure` (same pattern as levels 11/15/16) that writes an oversized, printable-ASCII-constrained symbolic buffer into the destination address — oversized enough to overflow past the buffer and into the saved return address.
2. Build the `simgr` with `save_unconstrained=True` and explicit stashes (`active`, `unconstrained`, `found`, `not_needed`) so unconstrained states are kept instead of discarded.
3. Manually loop: while there are active or unconstrained states and no solution yet, drain everything currently in `unconstrained` into `found`, then `.step()` the simulation forward.
4. Once a state is in `found`, constrain **that state's** `eip` to equal `print_good`'s address, then solve for the input.

**Why no per-state satisfiability check is needed here (unlike levels 15/16):** in 15/16 the pointer you were redirecting was symbolic *but already partially constrained* by earlier logic, so blindly committing a constraint risked landing on an unsatisfiable state. Here, `eip` is **freshly and completely unconstrained** the moment the return address is popped — by construction it can be *any* address, so `eip == print_good_address` is trivially satisfiable for every state that lands in `unconstrained`. That's why the scaffold can just drain the whole stash into `found` without filtering first, and only apply the real constraint afterward, on the state it ends up using.

**What you must find in your binary:**
- `print_good`'s address (`objdump -d ./binary | egrep print_good`).
- The exact `scanf` symbol your binary uses (e.g. `__isoc99_scanf`) and which argument position holds the destination buffer address.
- Roughly how large the symbolic buffer needs to be to guarantee it overflows into the saved return address (start large — 100+ bytes — and narrow down from your disassembly of the vulnerable function's stack frame if needed).

**Solution script:**
```python
import angr
import claripy
import sys

def main(argv):
    path_to_binary = argv[1]                          # ./17_angr_arbitrary_jump
    project = angr.Project(path_to_binary)

    initial_state = project.factory.entry_state()

    class ReplacementScanf(angr.SimProcedure):
        def run(self, format_string, scanf0_addr):
            # Oversized on purpose -- large enough to run past the buffer
            # and stomp the saved return address on the stack.
            password0 = claripy.BVS('password0', 100 * 8)     # <-- confirm/adjust size for your binary

            # Restrict to printable ASCII so the answer is actually typeable
            for char in password0.chop(bits=8):
                self.state.add_constraints(char >= 0x20, char <= 0x7e)

            # Write the symbolic buffer to the destination scanf() was given.
            # password0 is treated as a raw string, so endianness doesn't matter.
            self.state.memory.store(scanf0_addr, password0)

            self.state.globals['solution'] = password0

    scanf_symbol = '__isoc99_scanf'                        # <-- FIND THIS (confirm exact symbol in your binary)
    project.hook_symbol(scanf_symbol, ReplacementScanf())

    # save_unconstrained=True tells angr to keep (not discard) states whose
    # eip becomes fully symbolic, stashing them in 'unconstrained' instead.
    simulation = project.factory.simgr(
        initial_state,
        save_unconstrained=True,
        stashes={
            'active': [initial_state],
            'unconstrained': [],
            'found': [],
            'not_needed': []
        }
    )

    def has_found_solution():
        return simulation.found

    def has_unconstrained_to_check():
        return simulation.unconstrained

    def has_active():
        return simulation.active

    while (has_active() or has_unconstrained_to_check()) and (not has_found_solution()):
        for unconstrained_state in simulation.unconstrained:
            # eip is freshly unconstrained here, so every one of these states
            # can reach print_good -- no per-state satisfiability check needed,
            # unlike the arbitrary-read/write levels.
            simulation.move('unconstrained', 'found')
        simulation.step()

    if simulation.found:
        solution_state = simulation.found[0]

        print_good_address = 0x080484f6                    # <-- FIND THIS
        solution_state.add_constraints(solution_state.regs.eip == print_good_address)

        solution_password = solution_state.solver.eval(
            solution_state.globals['solution'],
            cast_to=bytes
        )
        print(solution_password)
    else:
        raise Exception('Could not find the solution')

if __name__ == '__main__':
    main(sys.argv)
```

**Hints:**
- By default angr **throws away** unconstrained states (assumes they're a dead end / not useful) — `save_unconstrained=True` is what makes this exploit search possible at all.
- Explicit stashes (`active`/`unconstrained`/`found`/`not_needed`) replace the automatic bucket that `simgr` normally manages for you — you are re-implementing a mini version of `.explore()`'s loop by hand.
- `simulation.move('unconstrained', 'found')` with no `filter_func` moves the **entire stash** in one call — that's intentional and sufficient here (see the satisfiability note above); the arbitrary-read/write levels needed per-state filtering because their symbolic pointers weren't freshly unconstrained.
- `self.state.globals[...]` is what lets you recover the symbolic password later — once you've replaced `scanf`, there's no ordinary stdin buffer to `posix.dumps()` from, exactly as in levels 11/15/16.
- Watch the `if __name__ == '__main__':` block — it must actually call `main(sys.argv)`, not just reference `main`; a scaffold that only says `main` (no call, no `sys.argv`) will silently do nothing when run.
