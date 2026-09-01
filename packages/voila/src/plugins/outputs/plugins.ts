/***************************************************************************
 * Copyright (c) 2018, Voilà contributors                                   *
 * Copyright (c) 2018, QuantStack                                           *
 *                                                                          *
 * Distributed under the terms of the BSD 3-Clause License.                 *
 *                                                                          *
 * The full license is in the file LICENSE, distributed with this software. *
 ****************************************************************************/
import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { PageConfig } from '@jupyterlab/coreutils';
import { IRenderMimeRegistry } from '@jupyterlab/rendermime';
import { Widget } from '@lumino/widgets';

import { VoilaApp } from '../../app';
import { RenderedCells } from './renderedcells';
import {
  createSkeleton,
  getExecutionURL,
  handleExecutionResult,
  createReactiveOutput,
  IExecutionMessage,
  IReceivedWidgetModel
} from './tools';
import { OutputAreaModel, SimplifiedOutputArea } from '@jupyterlab/outputarea';

const WIDGET_VIEW_MIMETYPE = 'application/vnd.jupyter.widget-view+json';

/**
 * The plugin that renders outputs.
 */
export const renderOutputsPlugin: JupyterFrontEndPlugin<void> = {
  id: '@voila-dashboards/voila:render-outputs',
  autoStart: true,
  requires: [IRenderMimeRegistry],
  activate: async (
    app: JupyterFrontEnd,
    rendermime: IRenderMimeRegistry
  ): Promise<void> => {
    app.started.then(() => {
      // TODO: Typeset a fake element to get MathJax loaded, remove this hack once
      // MathJax 2 is removed.
      rendermime.latexTypesetter?.typeset(document.createElement('div'));

      // Render latex in markdown cells
      const mdOutput = document.body.querySelectorAll('div.jp-MarkdownOutput');
      mdOutput.forEach((md) => {
        rendermime.latexTypesetter?.typeset(md as HTMLElement);
      });
      // Render code cell
      const cellOutputs = document.body.querySelectorAll(
        'script[type="application/vnd.voila.cell-output+json"]'
      );
      cellOutputs.forEach(async (cellOutput) => {
        const model = JSON.parse(cellOutput.innerHTML);

        const mimeType = rendermime.preferredMimeType(model.data, 'any');

        if (!mimeType) {
          return null;
        }
        const output = rendermime.createRenderer(mimeType);
        output.renderModel(model).catch((error) => {
          // Manually append error message to output
          const pre = document.createElement('pre');
          pre.textContent = `Javascript Error: ${error.message}`;
          output.node.appendChild(pre);

          // Remove mime-type-specific CSS classes
          pre.className = 'lm-Widget jp-RenderedText';
          pre.setAttribute('data-mime-type', 'application/vnd.jupyter.stderr');
        });

        output.addClass('jp-OutputArea-output');

        if (cellOutput.parentElement) {
          const container = cellOutput.parentElement;

          container.removeChild(cellOutput);

          // Attach output
          Widget.attach(output, container);
        }
      });

      const node = document.getElementById('rendered_cells');
      if (node) {
        const cells = new RenderedCells({ node });
        app.shell.add(cells, 'main');
      }
    });
  }
};

/**
 * The plugin that renders outputs.
 */
export const renderOutputsProgressivelyPlugin: JupyterFrontEndPlugin<void> = {
  id: '@voila-dashboards/voila:render-outputs-progressively',
  autoStart: true,
  requires: [IRenderMimeRegistry],
  activate: async (
    app: JupyterFrontEnd,
    rendermime: IRenderMimeRegistry
  ): Promise<void> => {
    const progressiveRendering =
      PageConfig.getOption('progressiveRendering') === 'true';
    if (!progressiveRendering) {
      return;
    }

    createSkeleton();

    const widgetManager = await (app as VoilaApp).widgetManagerPromise.promise;

    if (!widgetManager) {
      return;
    }

    const kernelId = widgetManager.kernel.id;

    const receivedWidgetModel: IReceivedWidgetModel = {};
    const modelRegisteredHandler = (_: any, modelId: string) => {
      if (receivedWidgetModel[modelId]) {
        const { outputModel, executionModel } = receivedWidgetModel[modelId];
        outputModel.add(executionModel);
        widgetManager.removeRegisteredModel(modelId);
      }
    };
    widgetManager.modelRegistered.connect(modelRegisteredHandler);
    const wsUrl = getExecutionURL(kernelId);
    const ws = new WebSocket(wsUrl);
    ws.onmessage = async (msg) => {
      const { action, payload }: IExecutionMessage = JSON.parse(msg.data);
      switch (action) {
        case 'execution_result': {
          const result = handleExecutionResult({
            payload,
            rendermime,
            widgetManager
          });
          for (const [key, val] of Object.entries(result)) {
            receivedWidgetModel[key] = val;
          }
          const { cell_index, total_cell } = payload;
          if (cell_index + 1 === total_cell) {
            // Executed all cells
            ws.close();
          }

          break;
        }
        case 'execution_error': {
          console.error(`Execution error: ${payload.error}`);
          break;
        }
        default:
          break;
      }
    };
    ws.onopen = () => {
      ws.send(
        JSON.stringify({ action: 'execute', payload: { kernel_id: kernelId } })
      );
    };
  }
};

/**
 * The plugin that renders outputs reactively.
 *
 * It behaves like the progressive rendering plugin, but additionally
 * re-executes the cell which rendered a widget (and all the cells below
 * it) whenever the widget is updated by the user. The websocket
 * connection is kept open after the full execution of the notebook to
 * allow these re-executions.
 */
export const renderOutputsReactivelyPlugin: JupyterFrontEndPlugin<void> = {
  id: '@voila-dashboards/voila:render-outputs-reactively',
  autoStart: true,
  requires: [IRenderMimeRegistry],
  activate: async (
    app: JupyterFrontEnd,
    rendermime: IRenderMimeRegistry
  ): Promise<void> => {
    const reactiveRendering =
      PageConfig.getOption('reactiveRendering') === 'true';
    if (!reactiveRendering) {
      return;
    }

    createSkeleton();

    const widgetManager = await (app as VoilaApp).widgetManagerPromise.promise;

    if (!widgetManager) {
      return;
    }

    const kernelId = widgetManager.kernel.id;

    const outputWidgets: { [cellIdx: number]: Array<{ widget: Widget, modelId?: string }> } = {};
    const reactiveWidgets: { [modelId: string]: { cellIdx: number, widget: Widget, hooked: boolean, pending?: boolean } } = {};
    let totalCell = 0;

    const ws = new WebSocket(getExecutionURL(kernelId));

    /**
     * Request the re-execution of the cell at `cellIndex` and all the
     * cells below it.
     */
    const requestReexecution = (cellIndex: number): void => {
      console.log(`Requesting re-execution of cell index ${cellIndex} and all cells below it.`);
      if (ws.readyState !== WebSocket.OPEN) {
        return;
      }
      const cellIds: number[] = [];
      for (let idx = cellIndex; idx < totalCell; idx++) {
        cellIds.push(idx);
      }
      ws.send(
        JSON.stringify({
          action: 'execute_cells',
          payload: { kernel_id: kernelId, cell_ids: cellIds }
        })
      );
    };

    /**
     * Register an event handler on a rendered widget model which listens
     * to updates made by the user and triggers the re-execution of the
     * cell which rendered the widget and all the cells below it.
     *
     * State updates made by widget views go through `save_changes`, while
     * custom messages (e.g. button clicks) go through `send`, so both are
     * hooked.
     */
    const hookWidgetModel = (modelId: string): void => {
      const cellIndex = reactiveWidgets[modelId]?.cellIdx;
      if (cellIndex === undefined || reactiveWidgets[modelId]?.hooked) {
        return;
      }
      if (!widgetManager.registeredModels.has(modelId)) {
        // The model is not registered yet, it will be hooked when the
        // `modelRegistered` signal is emitted.
        return;
      }
      console.log(`Hooking widget model ${modelId} for cell index ${cellIndex}`);
      reactiveWidgets[modelId].hooked = true;
      void widgetManager
        .get_model(modelId)
        .then((model) => {
          const saveChanges = model.save_changes.bind(model);
          model.save_changes = (
            ...args: Parameters<typeof model.save_changes>
          ) => {
            saveChanges(...args);
            requestReexecution(cellIndex);
          };
          const send = model.send.bind(model);
          model.send = (...args: Parameters<typeof model.send>) => {
            send(...args);
            requestReexecution(cellIndex);
          };
        })
        .catch((error) => {
          console.error(`Failed to hook widget model ${modelId}: ${error}`);
        });
    };

    widgetManager.modelRegistered.connect((_: any, modelId: string) => {
      hookWidgetModel(modelId);
    });

    ws.onmessage = async (msg) => {
      const { action, payload }: IExecutionMessage = JSON.parse(msg.data);
      switch (action) {
        case 'execution_result': {
          const { cell_index, total_cell } = payload;
          totalCell = total_cell;

          const element = document.querySelector(`[cell-index="${cell_index + 1}"]`);
          if (!element) {
            console.error(`No element found for cell index ${cell_index + 1}`);
            return;
          }

          const skeleton = element
            .getElementsByClassName('voila-skeleton-container')
            .item(0);
          if (skeleton) {
            element.removeChild(skeleton);
          }

          if (!payload.output_cell.outputs) {
            return;
          }
          if (payload.output_cell.outputs.length > 0) {
            element.lastElementChild?.classList.remove(
              'jp-mod-noOutputs',
              'jp-mod-noInput'
            );
          }

          if (outputWidgets[cell_index]) {
            console.log(`Clearing previous outputs for cell index ${cell_index + 1}`);
            for (const { widget, modelId } of outputWidgets[cell_index]) {
              if (modelId && reactiveWidgets[modelId]) {
                reactiveWidgets[modelId].pending = true;
                widget.node.style.display = 'none';
              } else {
                if (widget.isAttached) {
                  Widget.detach(widget);
                }
                widget.dispose();
              }
            }
            outputWidgets[cell_index] = [];
          }

          for (const outputData of payload.output_cell.outputs) {
            const modelId = (outputData?.data as any)?.[WIDGET_VIEW_MIMETYPE]?.model_id;
            console.log(`Processing output data for cell index ${cell_index + 1}, modelId: ${modelId}`);

            let model = null;
            let widget = null;

            if (!modelId) {
              // New raw output case.
              model = new OutputAreaModel({ trusted: true });
              widget = new SimplifiedOutputArea({
                model,
                rendermime
              });
              Widget.attach(widget, element as HTMLElement);
              model.add(outputData);
            } else if (reactiveWidgets[modelId] && reactiveWidgets[modelId].cellIdx === cell_index && reactiveWidgets[modelId].pending) {
              console.log("Reattaching existing widget.")
              widget = reactiveWidgets[modelId].widget;
              (element as any).moveBefore(widget.node, null);
              widget.node.style.display = '';
              reactiveWidgets[modelId].pending = false;
            } else {
              model = outputData;
              widget = createReactiveOutput({
                model: outputData,
                rendermime,
                parent: element as HTMLElement
              });

              if (!widget) {
                console.error(`Failed to create widget for cell index ${cell_index + 1}, modelId: ${modelId}`);
                continue;
              }

              reactiveWidgets[modelId] = { cellIdx: cell_index, widget, hooked: false };
              hookWidgetModel(modelId);
            }

            outputWidgets[cell_index] = outputWidgets[cell_index] || [];
            outputWidgets[cell_index].push({ widget, modelId });
          }

          for (const [modelId, { cellIdx, widget, pending }] of Object.entries(reactiveWidgets)) {
            if (pending && cellIdx === cell_index) {
              console.log(`Removing remaining pending widget for modelId ${modelId} at cell index ${cellIdx + 1}`);
              Widget.detach(widget);
              widget.dispose();
              delete reactiveWidgets[modelId];
            }
          }

          break;
        }
        case 'execution_error': {
          console.error(`Execution error: ${payload.error}`);
          break;
        }
        default:
          break;
      }
    };
    ws.onopen = () => {
      // Run all the cells when the websocket connection opens.
      ws.send(
        JSON.stringify({ action: 'execute', payload: { kernel_id: kernelId } })
      );
    };
  }
};
