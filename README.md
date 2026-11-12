# CodeQuiz

Kahoot-style live coding sessions: students write Java in the browser, you see every
submission, tests run when you close a question, you give points, everyone sees a leaderboard.
Quizzes are made in a built-in editor.

## Start

Double-click `start.bat`, or in this folder:

    python server.py              (port 8000)
    python server.py 8080         (another port, e.g. if 8000 is taken)

Needs Python 3 and a JDK (javac + java). The server prints the addresses:

| Page     | Address                       | Who                          |
|----------|-------------------------------|------------------------------|
| Students | `http://<laptop-ip>:8000`     | students, same Wi-Fi         |
| Host     | `http://localhost:8000/host`  | you, only on this laptop     |
| Editor   | `http://localhost:8000/editor`| you, only on this laptop     |
| Board    | `http://localhost:8000/board` | projector                    |

The laptop IP can change when you reconnect to Wi-Fi: restart the server, it shows the new one.

## Making a quiz (Editor)

- **+ New** makes a quiz. Everything saves automatically (top bar says *Saved ✓*).
- **Setup & rules** (optional): shown to students as a fold-out box on every question.
- **Given code**: classes students may use (e.g. `Room`). Compiled with every submission and
  shown to students. If a student writes a class with the same name, theirs is used.
- **Hidden test helpers**: code only the tests can use (e.g. a method that builds a labyrinth).
- Each **question** has a title, minutes, the class name students write, a **statement**
  (simple formatting: `- bullets`, `` `code` ``, `**bold**`, `> question box`, ```` ``` ```` code blocks)
  with **images** (Add image, paste a screenshot with Ctrl+V, or drag it in), **starter code**,
  and a **reference solution** (never shown to students).

### Tests

- **Output test**: runs the program's `main` with an input, compares what it prints.
  Choose how strictly: same lines, exactly the same, same words, any order, only numbers.
- **Code test**: a few lines of Java, e.g. `check(Solution.isPrime(7), true);`
  (see *How do tests work?* in the editor for all helpers).
- **Weight**: partial points = weighted share of passed tests, so hard tests count more.
- **Hidden**: students only see that it failed, not the input.
- **Time**: seconds before a test counts as too slow (endless loop / not efficient).
- **▶ Check tests with reference solution** runs everything on your solution, so you know the
  tests are right. **Fill empty expected outputs** copies your solution's output into output tests.

Each test gets fresh static fields, its own input/output and a big stack. A test that doesn't
compile against a student's code (wrong method name) fails alone. `System.exit` or an endless
loop only fails that one test.

## Running a session (Host)

Pick the quiz, **Use this quiz**, then ▶ on a question. While it's open, submissions are only
compiled (not run). **Student code only runs when the question closes** (time up or *Close now*),
or when you click *Compile & run tests* on one submission. Then grade with the buttons or keys
(1 correct, 2 by tests, 0 wrong, J/K next/previous), or *Grade all by tests*.
**Reload changes** takes the newest version from the editor without losing players or points.

## Saving and moving quizzes between laptops

- Each quiz is **one file** in `quizzes/` (`<id>.quiz.json`): text, images, tests and solutions.
  Files are written safely (a crash never leaves half a file).
- **Older versions** are kept automatically (`quizzes/.backups`, at most one per 3 minutes,
  the last 40), restore them with *Older versions…*. Deleted quizzes go to *Recently deleted*.
- **Export file** downloads the quiz; on the other laptop use **Import → From a file** (or drop
  the file on the editor). The file has a checksum: a damaged or hand-edited file gives a warning.
  If the quiz already exists there you choose: replace (old one kept as a backup) or keep both.
- **Share link** (same Wi-Fi, 30 minutes): paste it on the other laptop under Import.
- To keep the quizzes in a synced folder (OneDrive, USB stick, git repo):
  `python server.py --quizzes "D:\path\to\folder"`

Exported files contain the reference solutions: don't give them to students.

## Files

- `server.py`: the server (Python standard library only)
- `static/`: the pages (student, host, editor, board)
- `runner/`: the Java test runner (built automatically into `runner/build` on start)
- `quizzes/`: your quizzes
- `state.json`, `session_quiz.json`: the current session (players, submissions, points)
