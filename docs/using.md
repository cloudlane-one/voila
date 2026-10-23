% Copyright (c) 2018, Voilà Contributors
% Copyright (c) 2018, QuantStack
%
% Distributed under the terms of the BSD 3-Clause License.
%
% The full license is in the file LICENSE, distributed with this software.

(using)=

# Using Voilà

Voilà can be used as a standalone application, or as a Jupyter server
extension. This page describes how to do each. Before you begin, make
sure that you follow the steps in {ref}`install`.

The following sections cover how to use Voilà.

## As a standalone application

Voilà can be used to run, convert, and serve a Jupyter notebook as a
standalone app. This can be done via the command-line, with the following
pattern:

```bash
voila <path-to-notebook> <options>
```

For example, to render the `bqplot` example notebook as a standalone app, run

```bash
git clone https://github.com/voila-dashboards/voila
cd voila
voila notebooks/bqplot.ipynb
```

Voilà displays a message when your notebook-based application is live.
By default, Voilà runs at `localhost:8866`.

To serve a **directory of Jupyter Notebooks**, navigate to the directory
you'd like to serve, then simply run `voila`:

```bash
cd notebooks/
voila
```

The page served by Voilà will now contain a list of any notebooks in the
directory. By clicking on one, you will trigger Voilà's conversion process.
A new Jupyter kernel will be created for each notebook you click.

### Git status bar in the tree page

When the directory you serve is part of a git repository, the tree page can
show a small bar with the state of that repository:

- the current branch (or the commit, when the head is detached),
- the head commit and the tag of that commit, if it has one,
- the state of the worktree (clean, or how many changed files),
- how many commits the remote has that the branch does not, and their titles.

The bar also lets you switch to another local or remote branch and pull the
current one. Pulling is a merge, never a rebase, and it is refused while the
worktree has uncommitted changes; when it fails, for instance on a conflict,
the repository is put back where it was and the error git reported is shown.
There is no push, and no diff or history view.

The bar is off by default, because it exposes endpoints that run git in the
served directory. Turn it on with:

```bash
voila --VoilaConfiguration.git_ui=True
```

or, as a server extension:

```bash
jupyter server --ServerApp.jpserver_extensions="voila.server_extension=True" \
  --VoilaConfiguration.git_ui=True
```

The endpoints are only registered when the option is on, and they require an
authenticated user. Reads fetch the remote of the current branch so that the
number of commits to pull is not stale; a remote that cannot be reached is not
an error, the counts then describe the last successful fetch. Repository hooks
are disabled for the commands the bar runs. If git lives outside `PATH`, give
its path with `--VoilaConfiguration.git_executable`.

The bar is part of the JupyterLab-based tree page; the classic tree page
(`--VoilaConfiguration.classic_tree=True`) does not show it.

## As a Jupyter server extension

You can also use Voilà from within a Jupyter server (e.g., after running
`jupyter lab` or `jupyter notebook`).

:::{note}
Voilà can also be used as a notebook server extension, both with the
[notebook](https://github.com/jupyter/notebook) server or with the
[jupyter_server](https://github.com/jupyter/jupyter_server).
:::

To use Voilà within a pre-existing Jupyter server, first start the server,
then go to the following URL:

```bash
<url-of-my-server>/voila
```

For example, if you typed `jupyter lab` and it was running at
`http://localhost:8888/lab`, then Voilà would be accessed at
`http://localhost:8888/voila`.

In this case, Voilà will serve the directory in which the Jupyter
server was started.

## How does Voilà work?

When Voilà is run on a notebook, the following steps occur:

1. Voilà runs the code in the notebook and collects the outputs
2. The notebook and its outputs are converted to HTML. By default,
   the notebook **code cells are hidden**.
3. This page is served either as a Tornado application, or via the
   Jupyter server.
4. When users access the page, the widgets on the page have access to
   the underlying Jupyter kernel.

## The example notebooks

The [notebooks directory](https://github.com/voila-dashboards/voila/tree/main/notebooks)
contains a collection of Jupyter notebooks that can be rendered using Voilà:

- **basics.ipynb** - a notebook with interactions requiring a roundtrip to the kernel.
- **bqplot.ipynb** - uses custom Jupyter widgets such as
  [bqplot](https://github.com/bloomberg/bqplot).
- **dashboard.ipynb** - uses gridstack.js for the layout of each output.
- **gridspecLayout.ipynb** - uses
  [GridspecLayout](https://ipywidgets.readthedocs.io/en/latest/examples/Layout%20Templates.html#Grid-layout)
  for the layout of different widgets.
- **interactive.ipynb** - makes use of ipywidget's @interact.
- **ipympl.ipynb** - contains custom interactive matplotlib figures using the
  [ipympl](https://github.com/matplotlib/jupyter-matplotlib) widget.
- **ipyvolume.ipynb** - uses custom Jupyter widgets such as
  [ipyvolume](https://github.com/maartenbreddels/ipyvolume).
- **query-strings.ipynb** - uses HTTP query parameters to parametrize a notebook
- **xleaflet.ipynb** - a notebook that uses C++ kernel and interactive widgets

These examples demonstrate different interactive HTML widgets and can be used as inspiration
for getting started with Voilà.

To **run the example notebooks**, a few additional libraries can be installed using:

```bash
conda install -c conda-forge ipywidgets ipyvolume bqplot scipy
```

Or alternatively:

```bash
conda env create
```

The examples can then be served with:

```bash
cd notebooks/
voila
```
