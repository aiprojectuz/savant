"""DeepStream pipeline."""
from collections import defaultdict
from pathlib import Path
from queue import Queue
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import List, Optional
import time
import pyds

from pysavantboost import ObjectsPreprocessing
from pygstsavantframemeta import (
    add_convert_savant_frame_meta_pad_probe,
    nvds_frame_meta_get_nvds_savant_frame_meta,
)

from savant.deepstream.buffer_processor import (
    NvDsBufferProcessor,
    NvDsRawBufferProcessor,
    NvDsEncodedBufferProcessor,
)
from savant.deepstream.nvstreammux_config_builder import build_nvstreammux_config
from savant.gstreamer import Gst, GLib  # noqa:F401
from savant.gstreamer.codecs import Codec, CODEC_BY_NAME
from savant.gstreamer.pipeline import GstPipeline
from savant.deepstream.metadata import (
    nvds_obj_meta_output_converter,
    nvds_attr_meta_output_converter,
)
from savant.gstreamer.metadata import metadata_add_frame_meta, metadata_get_frame_meta
from savant.gstreamer.utils import on_pad_event, pad_to_source_id
from savant.deepstream.utils import (
    gst_nvevent_new_stream_eos,
    GST_NVEVENT_PAD_DELETED,
    gst_nvevent_parse_pad_deleted,
    gst_nvevent_parse_stream_eos,
    GST_NVEVENT_STREAM_EOS,
    nvds_frame_meta_iterator,
    nvds_obj_meta_iterator,
    nvds_attr_meta_iterator,
    nvds_remove_obj_attrs,
)
from savant.meta.constants import UNTRACKED_OBJECT_ID
from savant.utils.fps_meter import FPSMeter
from savant.utils.model_registry import ModelObjectRegistry
from savant.utils.source_info import SourceInfoRegistry, SourceInfo
from savant.utils.platform import is_aarch64
from savant.config.schema import PipelineElement, ModelElement
from savant.base.model import AttributeModel, ComplexModel
from savant.utils.sink_factories import SinkEndOfStream


class NvDsPipeline(GstPipeline):
    """Base class for managing the DeepStream Pipeline.

    :param name: Pipeline name
    :param source: Pipeline source element
    :param elements: Pipeline elements
    :key batch_size: Primary batch size (nvstreammux batch-size)
    :key output_frame: Whether to include frame in module output, not just metadata
    """

    def __init__(
        self,
        name: str,
        source: PipelineElement,
        elements: List[PipelineElement],
        **kwargs,
    ):
        self._batch_size = kwargs['batch_size']
        # Timeout in microseconds
        self._batched_push_timeout = kwargs.get('batched_push_timeout', 2000)

        self._max_parallel_streams = kwargs.get('max_parallel_streams', 64)

        # model artifacts path
        self._model_path = Path(kwargs['model_path'])

        self._source_adding_lock = Lock()
        self._sources = SourceInfoRegistry()

        # c++ preprocessing class
        self._objects_preprocessing = ObjectsPreprocessing()

        self._internal_attrs = set()

        output_frame = kwargs.get('output_frame')
        if output_frame:
            self._output_frame_codec = CODEC_BY_NAME[output_frame['codec']]
            self._output_frame_encoder_params = output_frame.get('encoder_params', {})
        else:
            self._output_frame_codec = None
            self._output_frame_encoder_params = None

        self._demuxer_src_pads: List[Gst.Pad] = []
        self._free_pad_indices: List[int] = []

        if source.element == 'zeromq_source_bin':
            output_alpha_channel = self._output_frame_codec in [
                Codec.PNG,
                Codec.RAW_RGBA,
            ]
            source.properties['convert-jpeg-to-rgb'] = output_alpha_channel
            source.properties['max-parallel-streams'] = self._max_parallel_streams

        super().__init__(name=name, source=source, elements=elements, **kwargs)

    def _build_buffer_processor(
        self,
        queue: Queue,
        fps_meter: FPSMeter,
    ) -> NvDsBufferProcessor:
        """Create buffer processor."""

        # model-object association storage
        model_object_registry = ModelObjectRegistry()

        if (
            self._output_frame_codec is None
            or self._output_frame_codec == Codec.RAW_RGBA
        ):
            return NvDsRawBufferProcessor(
                queue=queue,
                fps_meter=fps_meter,
                sources=self._sources,
                model_object_registry=model_object_registry,
                objects_preprocessing=self._objects_preprocessing,
                output_frame=self._output_frame_codec is not None,
            )
        return NvDsEncodedBufferProcessor(
            queue=queue,
            fps_meter=fps_meter,
            sources=self._sources,
            model_object_registry=model_object_registry,
            objects_preprocessing=self._objects_preprocessing,
            codec=self._output_frame_codec.value,
        )

    def _add_element(
        self,
        element: PipelineElement,
        with_probes: bool = False,
        link: bool = True,
    ) -> Gst.Element:
        if isinstance(element, ModelElement):
            if element.model.input.preprocess_object_tensor:
                self._objects_preprocessing.add_preprocessing_function(
                    element.name,
                    element.model.input.preprocess_object_tensor.custom_function,
                )
            if isinstance(element.model, (AttributeModel, ComplexModel)):
                for attr in element.model.output.attributes:
                    if attr.internal:
                        self._internal_attrs.add((element.name, attr.name))
        return super()._add_element(element=element, with_probes=with_probes, link=link)

    # Source
    def _add_source(self, source: PipelineElement):
        source.name = 'source'
        _source = self._add_element(source)
        _source.connect('pad-added', self.on_source_added)

        # Need to suppress EOS on nvstreammux sink pad
        # to prevent pipeline from shutting down
        self._suppress_eos = source.element == 'zeromq_source_bin'
        # nvstreammux is required for NvDs pipeline
        # add queue and set live-source for rtsp
        live_source = source.element == 'uridecodebin' and source.properties[
            'uri'
        ].startswith('rtsp://')
        if live_source:
            self._add_element(PipelineElement('queue'))
        self._create_muxer(live_source)

    # Sink
    def _add_sink(
        self,
        sink: Optional[PipelineElement] = None,
        link: bool = True,
        *data,
    ) -> Gst.Element:
        """Adds sink elements."""

        # FIXME: Temporarily disabled due to memory leak when using drawbin on jetson nx
        # add drawbin if frame should be in module output and there is no drawbin
        # if self._output_frame and 'drawbin' not in {
        #     e.element for e, _ in self.elements
        # }:
        #     self._add_element(DrawBinElement())

        self._create_demuxer(link)
        self._free_pad_indices = list(range(len(self._demuxer_src_pads)))

    # Input
    def on_source_added(  # pylint: disable=unused-argument
        self, element: Gst.Element, new_pad: Gst.Pad
    ):
        """Handle adding new video source.

        :param element: The source element that the pad was added to.
        :param new_pad: The pad that has been added.
        """

        # filter out non-video pads
        # new_pad caps can be None, e.g. for zeromq_source_bin
        caps = new_pad.get_current_caps()
        if caps and not caps.get_structure(0).get_name().startswith('video'):
            return

        # new_pad.name example `src_camera1` => source_id == `camera1` (real source_id)
        source_id = pad_to_source_id(new_pad)
        self._logger.debug(
            'Adding source %s. Pad name: %s.', source_id, new_pad.get_name()
        )

        try:
            source_info = self._sources.get_source(source_id)
        except KeyError:
            source_info = self._sources.init_source(source_id)
        else:
            while not source_info.lock.wait(5):
                self._logger.debug(
                    'Waiting source %s to release', source_info.source_id
                )
            source_info.lock.clear()

        self._logger.debug('Ready to add source %s', source_info.source_id)

        # Link elements to source pad only when caps are set
        new_pad.add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM,
            on_pad_event,
            {Gst.EventType.CAPS: self._on_source_caps},
            source_info,
        )

    def _on_source_caps(
        self, new_pad: Gst.Pad, event: Gst.Event, source_info: SourceInfo
    ):
        """Handle adding caps to video source pad."""

        new_pad_caps: Gst.Caps = event.parse_caps()
        self._logger.debug(
            'Pad %s.%s has caps %s',
            new_pad.get_parent().get_name(),
            new_pad.get_name(),
            new_pad_caps,
        )

        while source_info.pad_idx is None:
            try:
                with self._source_adding_lock:
                    source_info.pad_idx = self._free_pad_indices.pop(0)
            except IndexError:
                # avro_video_decode_bin already sent EOS for some stream and adding a
                # new one, but the former stream did not complete in this pipeline yet.
                self._logger.warning(
                    'Reached maximum number of streams: %s. '
                    'Waiting resources for source %s.',
                    self._max_parallel_streams,
                    source_info.source_id,
                )
                time.sleep(5)

        with self._source_adding_lock:
            self._sources.update_source(source_info)

            if not source_info.after_demuxer:
                self._add_source_output(source_info)
            input_src_pad = self._add_input_converter(
                new_pad,
                new_pad_caps,
                source_info,
            )
            add_convert_savant_frame_meta_pad_probe(
                input_src_pad,
                True,
            )
            self._link_to_muxer(input_src_pad, source_info)
            self._pipeline.set_state(Gst.State.PLAYING)

        self._logger.info('Added source %s', source_info.source_id)

        # Video source has been added, removing probe.
        return Gst.PadProbeReturn.REMOVE

    def _add_input_converter(
        self,
        new_pad: Gst.Pad,
        new_pad_caps: Gst.Caps,
        source_info: SourceInfo,
    ) -> Gst.Pad:
        nv_video_converter: Gst.Element = Gst.ElementFactory.make('nvvideoconvert')
        if not is_aarch64():
            nv_video_converter.set_property(
                'nvbuf-memory-type', int(pyds.NVBUF_MEM_CUDA_UNIFIED)
            )
        self._pipeline.add(nv_video_converter)
        nv_video_converter.sync_state_with_parent()
        video_converter_sink: Gst.Pad = nv_video_converter.get_static_pad('sink')
        if not video_converter_sink.query_accept_caps(new_pad_caps):
            self._logger.debug(
                '"nvvideoconvert" cannot accept caps %s.'
                'Inserting "videoconvert" before it.',
                new_pad_caps,
            )
            video_converter: Gst.Element = Gst.ElementFactory.make('videoconvert')
            self._pipeline.add(video_converter)
            video_converter.sync_state_with_parent()
            assert video_converter.link(nv_video_converter)
            video_converter_sink = video_converter.get_static_pad('sink')
            source_info.before_muxer.append(video_converter)

        source_info.before_muxer.append(nv_video_converter)
        # TODO: send EOS to video_converter on unlink if source didn't
        assert new_pad.link(video_converter_sink) == Gst.PadLinkReturn.OK

        capsfilter: Gst.Element = Gst.ElementFactory.make('capsfilter')
        capsfilter.set_property(
            'caps', Gst.Caps.from_string('video/x-raw(memory:NVMM), format=RGBA')
        )
        capsfilter.set_state(Gst.State.PLAYING)
        self._pipeline.add(capsfilter)
        source_info.before_muxer.append(capsfilter)
        assert nv_video_converter.link(capsfilter)

        return capsfilter.get_static_pad('src')

    def _remove_input_elems(
        self,
        source_info: SourceInfo,
        sink_pad: Gst.Pad,
    ):
        self._logger.debug(
            'Removing input elements for source %s', source_info.source_id
        )
        for elem in source_info.before_muxer:
            self._logger.debug('Removing element %s', elem.get_name())
            elem.set_locked_state(True)
            elem.set_state(Gst.State.NULL)
            self._pipeline.remove(elem)
        source_info.before_muxer = []
        self._release_muxer_sink_pad(sink_pad)
        self._logger.debug(
            'Input elements for source %s removed', source_info.source_id
        )
        return False

    # Output
    def _add_source_output(self, source_info: SourceInfo):
        fakesink = super()._add_sink(
            PipelineElement(
                element='fakesink',
                name=f'sink_{source_info.source_id}',
                properties={
                    'sync': 0,
                    'qos': 0,
                    'enable-last-sample': 0,
                },
            ),
            False,
            source_info,
        )
        fakesink.sync_state_with_parent()

        fakesink_pad: Gst.Pad = fakesink.get_static_pad('sink')
        fakesink_pad.add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM,
            on_pad_event,
            {Gst.EventType.EOS: self.on_last_pad_eos},
            source_info,
        )

        output_queue = self._add_element(PipelineElement('queue'), link=False)
        output_queue.sync_state_with_parent()
        source_info.after_demuxer.append(output_queue)
        self._link_demuxer_src_pad(output_queue.get_static_pad('sink'), source_info)

        if (
            self._output_frame_codec is not None
            and self._output_frame_codec != Codec.RAW_RGBA
        ):
            add_convert_savant_frame_meta_pad_probe(
                output_queue.get_static_pad('src'),
                False,
            )
            output_converter = self._add_element(
                PipelineElement(
                    'nvvideoconvert',
                    properties=(
                        {}
                        if is_aarch64()
                        else {'nvbuf-memory-type': int(pyds.NVBUF_MEM_CUDA_UNIFIED)}
                    ),
                ),
            )
            source_info.after_demuxer.append(output_converter)
            output_converter.sync_state_with_parent()

            encoder = self._add_element(
                PipelineElement(
                    self._output_frame_codec.value.encoder,
                    properties=self._output_frame_encoder_params,
                )
            )
            source_info.after_demuxer.append(encoder)
            encoder.sync_state_with_parent()

            # A parser for codecs h264, h265 is added to include
            # the Sequence Parameter Set (SPS) and the Picture Parameter Set (PPS)
            # to IDR frames in the video stream. SPS and PPS are needed
            # to correct recording or playback not from the beginning
            # of the video stream.
            if self._output_frame_codec.value.parser in ['h264parse', 'h265parse']:
                parser = self._add_element(
                    PipelineElement(
                        self._output_frame_codec.value.parser,
                        properties=({'config-interval': -1}),
                    )
                )
                source_info.after_demuxer.append(parser)
                parser.sync_state_with_parent()
                assert parser.link(fakesink)
            else:
                assert encoder.link(fakesink)
        else:
            assert output_queue.link(fakesink)

        source_info.after_demuxer.append(fakesink)

    def _remove_output_elems(self, source_info: SourceInfo):
        """Process EOS on last pad."""
        self._logger.debug(
            'Removing output elements for source %s', source_info.source_id
        )
        for elem in source_info.after_demuxer:
            self._logger.debug('Removing element %s', elem.get_name())
            elem.set_locked_state(True)
            elem.set_state(Gst.State.NULL)
            self._pipeline.remove(elem)
        source_info.after_demuxer = []
        self._logger.debug(
            'Output elements for source %s removed', source_info.source_id
        )

        self._sources.remove_source(source_info)

        self._free_pad_indices.append(source_info.pad_idx)
        source_info.pad_idx = None
        self._logger.debug('Releasing lock for source %s', source_info.source_id)
        source_info.lock.set()
        self._logger.info(
            'Resources for source %s has been released.', source_info.source_id
        )
        return False

    def on_last_pad_eos(self, pad: Gst.Pad, event: Gst.Event, source_info: SourceInfo):
        """Process EOS on last pad."""
        self._logger.debug(
            'Got EOS on pad %s.%s', pad.get_parent().get_name(), pad.get_name()
        )
        GLib.idle_add(self._remove_output_elems, source_info)

        self._queue.put(SinkEndOfStream(source_info.source_id))

        return (
            Gst.PadProbeReturn.DROP if self._suppress_eos else Gst.PadProbeReturn.PASS
        )

    def update_frame_meta(self, pad: Gst.Pad, info: Gst.PadProbeInfo):
        """Prepare frame meta for output."""
        buffer: Gst.Buffer = info.get_buffer()
        nvds_batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buffer))

        # convert output meta
        for nvds_frame_meta in nvds_frame_meta_iterator(nvds_batch_meta):
            frame_width = nvds_frame_meta.source_frame_width
            frame_height = nvds_frame_meta.source_frame_height
            # use consecutive numbers for object_id in case there is no tracker
            object_ids = defaultdict(int)
            # first iteration to correct object_id
            for nvds_obj_meta in nvds_obj_meta_iterator(nvds_frame_meta):
                # correct object_id (track_id)
                if nvds_obj_meta.object_id == UNTRACKED_OBJECT_ID:
                    nvds_obj_meta.object_id = object_ids[nvds_obj_meta.obj_label]
                    object_ids[nvds_obj_meta.obj_label] += 1

            # will extend source metadata
            source_id = self._sources.get_id_by_pad_index(nvds_frame_meta.pad_index)
            savant_frame_meta = nvds_frame_meta_get_nvds_savant_frame_meta(
                nvds_frame_meta
            )
            frame_idx = savant_frame_meta.idx if savant_frame_meta else None
            frame_pts = nvds_frame_meta.buf_pts
            frame_meta = metadata_get_frame_meta(source_id, frame_idx, frame_pts)

            # second iteration to collect module objects
            for nvds_obj_meta in nvds_obj_meta_iterator(nvds_frame_meta):
                # skip fake primary frame object
                if nvds_obj_meta.obj_label == 'frame':
                    continue

                obj_meta = nvds_obj_meta_output_converter(
                    nvds_obj_meta, frame_width, frame_height
                )
                for attr_meta_list in nvds_attr_meta_iterator(
                    frame_meta=nvds_frame_meta, obj_meta=nvds_obj_meta
                ):
                    for attr_meta in attr_meta_list:
                        if (
                            attr_meta.element_name,
                            attr_meta.name,
                        ) not in self._internal_attrs:
                            obj_meta['attributes'].append(
                                nvds_attr_meta_output_converter(attr_meta)
                            )
                nvds_remove_obj_attrs(nvds_frame_meta, nvds_obj_meta)
                frame_meta.metadata['objects'].append(obj_meta)

            metadata_add_frame_meta(source_id, frame_idx, frame_pts, frame_meta)

        return Gst.PadProbeReturn.PASS

    # Muxer
    def _create_muxer(self, live_source: bool) -> Gst.Element:
        """Create nvstreammux element and add it into pipeline.

        :param live_source: Whether source is live or not.
        """

        nvstreammux_config = build_nvstreammux_config(
            batch_size=self._batch_size,
            max_same_source_frames=self._batch_size,
            adaptive_batching=False,
        )
        self._logger.debug('Generated nvstreammux config:\n%s', nvstreammux_config)
        nvstreammux_config_file = NamedTemporaryFile(
            prefix='nvstreammux_config_', suffix='.txt'
        )
        with open(nvstreammux_config_file.name, 'w') as f:
            f.write(nvstreammux_config)
        self._logger.info(
            'Saved nvstreammux config to %s', nvstreammux_config_file.name
        )
        frame_processing_parameters = {
            'config-file-path': nvstreammux_config_file.name,
            # TODO:
            # Allowed range for batch-size: 1 - 1024
            # Allowed range for buffer-pool-size: 4 - 1024
            # 'buffer-pool-size': max(4, self._batch_size),
            # 'batched-push-timeout': self._batched_push_timeout,
            # 'live-source': live_source,  # True for RTSP or USB camera
            # TODO: remove when the bug with odd will be fixed
            # https://forums.developer.nvidia.com/t/nvstreammux-error-releasing-cuda-memory/219895/3
            # 'interpolation-method': 6,
        }
        muxer = self._add_element(
            PipelineElement(
                element='nvstreammux',
                name='muxer',
                properties=frame_processing_parameters,
            )
        )
        self._logger.info(
            'Pipeline frame processing parameters: %s.', frame_processing_parameters
        )
        # input processor (post-muxer)
        muxer_src_pad: Gst.Pad = muxer.get_static_pad('src')
        muxer_src_pad.add_probe(
            Gst.PadProbeType.BUFFER,
            self._buffer_processor.input_probe,
        )
        if self._suppress_eos:
            muxer_src_pad.add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM,
                on_pad_event,
                {GST_NVEVENT_PAD_DELETED: self._on_muxer_sink_pad_deleted},
            )

        return muxer

    def _link_to_muxer(self, pad: Gst.Pad, source_info: SourceInfo):
        """Link src pad to muxer.

        :param pad: Src pad to connect.
        :param source_info: Video source info.
        """

        muxer_sink_pad = self._request_muxer_sink_pad(source_info)
        assert pad.link(muxer_sink_pad) == Gst.PadLinkReturn.OK

    def _request_muxer_sink_pad(self, source_info: SourceInfo) -> Gst.Pad:
        """Request sink pad from muxer.

        :param source_info: Video source info.
        """

        muxer: Gst.Element = self._pipeline.get_by_name('muxer')
        # sink_N == NvDsFrameMeta.pad_index
        sink_pad_name = f'sink_{source_info.pad_idx}'
        sink_pad: Gst.Pad = muxer.get_static_pad(sink_pad_name)
        if sink_pad is None:
            self._logger.debug(
                'Requesting new sink pad on %s: %s', muxer.get_name(), sink_pad_name
            )
            sink_pad: Gst.Pad = muxer.get_request_pad(sink_pad_name)
            sink_pad.add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM,
                on_pad_event,
                {Gst.EventType.EOS: self._on_muxer_sink_pad_eos},
                source_info.source_id,
            )

        return sink_pad

    def _release_muxer_sink_pad(self, pad: Gst.Pad):
        """Release sink pad of muxer.

        :param pad: Sink pad to release.
        """

        muxer: Gst.Element = pad.get_parent()
        self._logger.debug(
            'Releasing pad %s.%s',
            pad.get_parent().get_name(),
            pad.get_name(),
        )
        # Releasing muxer.sink pad to trigger nv-pad-deleted event on muxer.src pad
        muxer.release_request_pad(pad)

    def _on_muxer_sink_pad_eos(self, pad: Gst.Pad, event: Gst.Event, source_id: str):
        """Processes EOS event on muxer sink pad."""

        self._logger.debug(
            'Got EOS on pad %s.%s', pad.get_parent().get_name(), pad.get_name()
        )
        source_info = self._sources.get_source(source_id)
        GLib.idle_add(self._remove_input_elems, source_info, pad)
        return (
            Gst.PadProbeReturn.DROP if self._suppress_eos else Gst.PadProbeReturn.PASS
        )

    def _on_muxer_sink_pad_deleted(self, pad: Gst.Pad, event: Gst.Event):
        """Send GST_NVEVENT_STREAM_EOS event before GST_NVEVENT_PAD_DELETED.

        GST_NVEVENT_STREAM_EOS is needed to flush data on downstream
        elements.
        """

        pad_idx = gst_nvevent_parse_pad_deleted(event)
        self._logger.debug(
            'Got GST_NVEVENT_PAD_DELETED with source-id %s on %s.%s',
            pad_idx,
            pad.get_parent().get_name(),
            pad.get_name(),
        )
        nv_eos_event = gst_nvevent_new_stream_eos(pad_idx)
        pad.push_event(nv_eos_event)
        return Gst.PadProbeReturn.PASS

    # Demuxer
    def _create_demuxer(self, link: bool) -> Gst.Element:
        """Create nvstreamdemux element and add it into pipeline.

        :param link: Whether to automatically link demuxer to the last pipeline element.
        """

        demuxer = self._add_element(
            PipelineElement(
                element='nvstreamdemux',
                name='demuxer',
            ),
            link=link,
        )
        self._demuxer_src_pads = self._allocate_demuxer_pads(
            demuxer, self._max_parallel_streams
        )
        demuxer.get_static_pad('sink').get_peer().add_probe(
            Gst.PadProbeType.BUFFER, self.update_frame_meta
        )
        return demuxer

    def _allocate_demuxer_pads(self, demuxer: Gst.Element, n_pads: int):
        """Allocate a fixed number of demuxer src pads."""

        pads = []
        for pad_idx in range(n_pads):
            pad: Gst.Pad = demuxer.get_request_pad(f'src_{pad_idx}')
            pad.add_probe(
                Gst.PadProbeType.EVENT_DOWNSTREAM,
                on_pad_event,
                {GST_NVEVENT_STREAM_EOS: self._on_demuxer_src_pad_eos},
            )
            pads.append(pad)
        return pads

    def _on_demuxer_src_pad_eos(self, pad: Gst.Pad, event: Gst.Event):
        """Processes EOS events on demuxer src pad."""

        pad_idx = gst_nvevent_parse_stream_eos(event)
        if pad_idx is None or pad != self._demuxer_src_pads[pad_idx]:
            # nvstreamdemux redirects GST_NVEVENT_STREAM_EOS on each src pad
            return Gst.PadProbeReturn.PASS
        self._logger.debug(
            'Got GST_NVEVENT_STREAM_EOS on %s.%s',
            pad.get_parent().get_name(),
            pad.get_name(),
        )
        peer: Gst.Pad = pad.get_peer()
        if peer is not None:
            self._logger.debug(
                'Unlinking %s.%s from %s.%s',
                peer.get_parent().get_name(),
                peer.get_name(),
                pad.get_parent().get_name(),
                pad.get_name(),
            )
            pad.unlink(peer)
            self._logger.debug(
                'Sending EOS to %s.%s',
                peer.get_parent().get_name(),
                peer.get_name(),
            )
            peer.send_event(Gst.Event.new_eos())
        return Gst.PadProbeReturn.DROP

    def _link_demuxer_src_pad(self, pad: Gst.Pad, source_info: SourceInfo):
        """Link demuxer src pad to some sink pad.

        :param pad: Connect demuxer src pad to this sink pad.
        :param source_info: Video source info.
        """

        demuxer_src_pad = self._demuxer_src_pads[source_info.pad_idx]
        assert demuxer_src_pad.link(pad) == Gst.PadLinkReturn.OK
