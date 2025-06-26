import os
import sys
from pathlib import Path
import shutil
from typing import Protocol, List, Optional, Callable
import SimpleITK as sitk

import qt
import slicer

from .Parameter import Parameter
from .Signal import Signal


def copyfile(src,dst):
    img = sitk.ReadImage(src)
    sitk.WriteImage(img,dst)

class SegmentationLogicProtocol(Protocol):
    """
    Segmentation interface class.
    This class is used as interface which is expected by the NNUnet Widget.
    Any instance implementing this interface is compatible with the NNUnet Widget.
    """
    inferenceFinished: Signal
    errorOccurred: Signal
    progressInfo: Signal

    def setParameter(self, nnUNetParam: Parameter) -> None:
        pass

    def startSegmentation(
            self,
            volumeNode: "slicer.vtkMRMLScalarVolumeNode"
    ) -> None:
        pass

    def stopSegmentation(self):
        pass

    def waitForSegmentationFinished(self):
        pass

    def loadSegmentation(self) -> "slicer.vtkMRMLSegmentationNode":
        pass


class ProcessProtocol(Protocol):
    """
    Interface for NNUnet process runner.
    Process is responsible for running the NNUnet prediction and reporting progress / errors.
    """

    errorOccurred: Signal
    finished: Signal
    readInfo: Signal

    def start(self, program: str, args: List[str]) -> None:
        pass

    def stop(self) -> None:
        pass

    def waitForFinished(self) -> None:
        pass


class SegmentationLogic:
    r"""
    Segmentation logic for nnUNet based segmentations.

    This class is responsible for writing a volume file with correct nnUNet formatting, calling the nnUNet detection
    on the given folder with user inputs and user model and allowing to load the generated segmentation afterwards.

    At the start of the nnUNet detection, a temporary folder is created with the selected input volume.
    The nnUNet detection QProcess is called with user parameters.

    Once the QProcess processing is done (either successfully or not), the Segmentation Logic triggers
    the inferenceFinished call.

    During execution, the standard output is returned using the progressInfo signal. If any error occurs, the
    errorOccurred sigal is used.

    The loadSegmentation method can be called to load the segmentation results. If the segmentation failed, this method
    call will raise a RuntimeError Exception.

    Usage example :
    >>> from SlicerNNUNetLib import SegmentationLogic, Parameter
    >>>
    >>> # Create instance
    >>> logic = SegmentationLogic()
    >>>
    >>> # Connect progress and error logging
    >>> logic.progressInfo.connect(print)
    >>> logic.errorOccurred.connect(slicer.util.errorDisplay)
    >>>
    >>> # Connect processing done signal
    >>> logic.inferenceFinished.connect(logic.loadSegmentation)
    >>>
    >>> # Start segmentation on given volume node on 5 folds
    >>> param = Parameter(modelPath=Path(r"C:\<PATH_TO>\NNUnetModel\Dataset123_456MRI"), folds = "0,1,2,3,4")
    >>> import SampleData
    >>> volumeNode = SampleData.downloadSample("MRHead")
    >>> logic.setParameter(param)
    >>> logic.startSegmentation(volumeNode)
    """

    def __init__(self, process: Optional[ProcessProtocol] = None):
        self.inferenceFinished = Signal()
        self.errorOccurred = Signal("str")
        self.progressInfo = Signal("str")

        self.inferenceProcess = process or Process(qt.QProcess.MergedChannels)
        self.inferenceProcess.finished.connect(self.inferenceFinished)
        self.inferenceProcess.errorOccurred.connect(self.errorOccurred)
        self.inferenceProcess.readInfo.connect(self.progressInfo)

        self._nnUNet_predict_path = None
        self._nnUNetParam: Optional[Parameter] = None
        self._tmpDir = qt.QTemporaryDir()

    def __del__(self):
        self.stopSegmentation()

    def setParameter(self, nnUnetConf: Parameter):
        self._nnUNetParam = nnUnetConf

    def startSegmentation(self, volumeNode: "slicer.vtkMRMLScalarVolumeNode") -> None:
        """Run the segmentation on a slicer volumeNode, get the result as a segmentationNode"""
        # Check the nnUNet parameters are correct
        try:
            self._getNNUNetParamArgsOrRaise()
        except RuntimeError as e:
            self.errorOccurred(e)
            return

        # Prepare the inference directory
        if not self._prepareInferenceDir(volumeNode):
            self.errorOccurred(f"Failed to export volume node to {self.nnUNetInDir}")
            return

        # Launch the nnUNet processing
        self._startInferenceProcess()

    def stopSegmentation(self):
        self.inferenceProcess.stop()

    def waitForSegmentationFinished(self):
        self.inferenceProcess.waitForFinished()

    def loadSegmentation(self) -> "slicer.vtkMRMLSegmentationNode":
        try:
            segmentationNode = slicer.util.loadSegmentation(self._outFile)
            self._renameSegments(segmentationNode)
            return segmentationNode
        except StopIteration:
            raise RuntimeError(
                "Failed to load the segmentation.\n"
                "Something went wrong during the nnUNet processing.\n"
                "Please check the logs for potential errors and contact the library maintainers."
            )
        
    def moveSegmentationFromNNUNetToFolder(self,outputFolder):
        self.progressInfo(f"Transferring nnUNet results in {self._tmpDir.path()}\n")
        for idx, data in enumerate(self.d):
            name = data['raw'].split('.')[0]
            copyfile(self.nnUNetOutDir.joinpath(data['nnUNet']),os.path.join(outputFolder,name+'.seg.nrrd'))
            self.progressInfo(f"{idx+1}/{len(self.d)} Segmentation have been Transferred\n")

    def _renameSegments(self, segmentationNode: "slicer.vtkMRMLSegmentationNode") -> None:
        """
        Rename loaded segments with dataset file labels dict.
        """
        labels = self._nnUNetParam.readSegmentIdsAndLabelsFromDatasetFile()
        if labels is None:
            return

        for segmentId, label in labels:
            segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
            if segment is None:
                continue
            segment.SetName(label)

    @staticmethod
    def _nnUNetPythonDir():
        return Path(sys.executable).parent.joinpath("..", "lib", "Python")

    @classmethod
    def _findUNetPredictPath(cls):
        # nnUNet install dir depends on OS. For Windows, install will be done in the Scripts dir.
        # For Linux and MacOS, install will be done in the bin folder.
        nnUNetPaths = ["Scripts", "bin"]
        for path in nnUNetPaths:
            predict_paths = list(sorted(cls._nnUNetPythonDir().joinpath(path).glob("nnUNetv2_predict*")))
            if predict_paths:
                return predict_paths[0].resolve()

        return None

    def _startInferenceProcess(self):
        """
        Run the nnU-Net V2 inference script
        """
        # Check the nnUNet predict script is correct
        nnUnetPredictPath = self._findUNetPredictPath()
        if not nnUnetPredictPath:
            self.errorOccurred("Failed to find nnUNet predict path.")
            return

        # Get the nnUNet parameters as arg list
        try:
            args = self._getNNUNetParamArgsOrRaise()
        except RuntimeError as e:
            self.errorOccurred(e)
            return

        # setup environment variables
        # not needed, just needs to be an existing directory
        os.environ['nnUNet_preprocessed'] = self._nnUNetParam.modelFolder.as_posix()

        # not needed, just needs to be an existing directory
        os.environ['nnUNet_raw'] = self._nnUNetParam.modelFolder.as_posix()
        os.environ['nnUNet_results'] = self._nnUNetParam.modelFolder.as_posix()

        argListStr = ' '.join(str(a) for a in args)
        self.progressInfo(
            "Starting nnUNet with the following parameters:\n"
            f"\n{nnUnetPredictPath} {argListStr}\n\n"
            "JSON parameters :\n"
            f"{self._nnUNetParam.debugString()}\n"
        )
        self.progressInfo("nnUNet preprocessing...\n")
        self.inferenceProcess.start(nnUnetPredictPath, args, qt.QProcess.Unbuffered | qt.QProcess.ReadOnly)

    def _getNNUNetParamArgsOrRaise(self):
        return self._nnUNetParam.asArgList(self.nnUNetInDir, self.nnUNetOutDir)

    @property
    def _outFile(self) -> str:
        return next(file for file in self.nnUNetOutDir.rglob(f"*{self._fileEnding}")).as_posix()

    def _prepareInferenceDir(self, volumeNode) -> bool:
        self._tmpDir.remove()
        self.nnUNetOutDir.mkdir(parents=True)
        self.nnUNetInDir.mkdir(parents=True)

        # Name of the volume should match expected nnUNet conventions
        self.progressInfo(f"Transferring volume to nnUNet in {self._tmpDir.path()}\n")
        return self._preprareInferenceDirBatch(volumeNode) if isinstance(volumeNode,str) else self._prepareInferenceDirVolume(volumeNode)
    
    def _prepareInferenceDirVolume(self,volumeNode):
        volumePath = self.nnUNetInDir.joinpath(f"volume_0000{self._fileEnding}")
        slicer.util.exportNode(volumeNode, volumePath)
        return volumePath.exists()
    
    def _preprareInferenceDirBatch(self, folderPath) -> bool :
        listVolumePath = [volumePath for volumePath in os.listdir(folderPath) if os.path.isfile(os.path.join(folderPath,volumePath))]

        self.progressInfo(f"0/{len(listVolumePath)} Volume are transferring to nnUNet\n")
        self.d = []
        _fileEnding = self._fileEnding
        for idx, volumePath in enumerate(listVolumePath):
            newName = f'Volume_{idx}_0000{self._fileEnding}'
            nameSeg = f'Volume_{idx}{self._fileEnding}'
            self.d.append({
                'raw':volumePath,
                'nnUNet':nameSeg
            })
            extension = '.'.join(volumePath.split('.')[1:])
            if extension == _fileEnding:
                shutil.copyfile(os.path.join(folderPath,volumePath), self.nnUNetInDir.joinpath(newName))
            else :
                copyfile(os.path.join(folderPath,volumePath),self.nnUNetInDir.joinpath(newName))
            self.progressInfo(f"{idx+1}/{len(listVolumePath)} Volume are transferring to nnUNet\n")
        return len(listVolumePath)

    @property
    def _fileEnding(self):
        return self._nnUNetParam.readFileEndingFromDatasetFile() if self._nnUNetParam else ".nii.gz"

    @property
    def nnUNetOutDir(self):
        return Path(self._tmpDir.path()).joinpath("output")

    @property
    def nnUNetInDir(self):
        return Path(self._tmpDir.path()).joinpath("input")


class Process:
    """
    Convenience wrapper around QProcess run.
    Forwards the process read / error events when read.
    Kills process on stop if process is running.
    """

    def __init__(self, channelMode: qt.QProcess.ProcessChannelMode):
        self.errorOccurred = Signal("str")
        self.finished = Signal()
        self.readInfo = Signal("str")

        self.process = qt.QProcess()
        self.process.setProcessChannelMode(channelMode)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self._onErrorOccurred)
        self.process.readyRead.connect(self._onReadyRead)

    def stop(self):
        if self.process.state() == self.process.Running:
            self.readInfo("Killing process.")
            self.process.kill()

    def start(self, program, args, openMode: qt.QIODevice.OpenMode):
        self.stop()
        self.process.start(program, args, openMode)

    def waitForFinished(self, timeOut_ms: Optional[int] = None):
        self.process.waitForFinished(timeOut_ms if timeOut_ms is not None else -1)

    def _onReadyRead(self):
        self._report(self.process.readAll(), self.readInfo)

    def _onErrorOccurred(self, *_):
        self._report(self.process.readAllStandardError(), self.errorOccurred)

    @staticmethod
    def _report(stream: "qt.QByteArray", outSignal: Callable[[str], None]) -> None:
        info = qt.QTextCodec.codecForUtfText(stream).toUnicode(stream)
        if info:
            outSignal(info)



