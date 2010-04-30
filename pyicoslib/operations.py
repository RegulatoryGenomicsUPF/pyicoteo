"""
Pyicos is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import os.path
import sys
import os
import shutil
import logging
import math
from collections import defaultdict
from heapq import heappop, heappush
from itertools import islice, cycle
from tempfile import gettempdir
import linecache
from datetime import datetime

from core import Cluster, Region, InvalidLine, InsufficientData, ConversionNotSupported, BED, ELAND, PK, WIG, SPK, CLUSTER_FORMATS, VARIABLE_WIG, READ_FORMATS, WRITE_FORMATS
from tempfile import gettempdir

Normalize = 'normalize'
Extend = 'extend'
Subtract = 'subtract'
RemoveChromosome = 'remove_chromosome'
Split = 'split'
Trim = 'trim'
Cut = 'filter'
Poisson = 'poisson'
Convert = 'convert'
NoWrite = 'nowrite'
DiscardArtifacts = 'discard'
RemoveRegion = 'remove'
RemoveDuplicates = 'remove_duplicates'
ModFDR = 'modfdr'

class OperationFailed(Exception):
    pass

class Utils:
    @staticmethod
    def add_slash_to_path(path):
        if path[-1] != '/':
            path = '%s/'%path
        return path
    

    @staticmethod
    def poisson(actual, mean):
        '''From StackOverflow: This algorithm is iterative,
            to keep the components from getting too large or small'''
        try:
            p = math.exp(-mean)
            for i in xrange(actual):
                p *= mean
                p /= i+1
            return p
        
        except OverflowError:
            return 0

    class BigSort:
        """
        This class can sort huge files without loading them fully into memory.
        Based on a recipe by Tomasz Bieruta found at http://code.activestate.com/recipes/415581/

        NOTE: This class is becoming a preprocessing class for the signal. This is a good thing, I think! But its not
        only a sorting class then. We have to think about renaming it, or extracting functionality from it...
        """
        def __init__(self, file_format=None, read_half_open=False, extension=0, id=0):
            self.file_format = file_format
            self.extension = extension
            if self.file_format:
                self.cluster = Cluster(read=self.file_format, write=self.file_format, read_half_open=read_half_open, write_half_open=read_half_open)
            self.id = id
            
        def skipHeaderLines(self, key, input_file):
            validLine = False
            count = 0
            while not validLine and count < 40:
                try:
                    currentPos = input_file.tell()
                    line = [input_file.readline()]
                    line.sort(key=key)
                    input_file.seek(currentPos)
                    validLine = True
                except:
                    print 'Skipping header line...'
                    count += 1

        def filter_chunk(self, chunk):
            filtered_chunk = []
            for line in chunk:
                if self.file_format != ELAND or self.cluster.reader.eland_quality_filter(line):
                    self.cluster.clear()
                    self.cluster.read_line(line)
                    self.cluster.extend(self.extension)
                    if not self.cluster.is_empty():
                        filtered_chunk.append(self.cluster.write_line())
            
            return filtered_chunk

        def sort(self, input,output=None,key=None,buffer_size=40000,tempdirs=[], tempFileSize=512*1024, filter=False):
            if key is None:
                key = lambda x : x

            if not tempdirs:
                tempdirs.append(gettempdir())
            input_file = file(input,'rb',tempFileSize)
            self.skipHeaderLines(key, input_file)
            try:
                input_iterator = iter(input_file)
                chunks = []
                for tempdir in cycle(tempdirs):
                    current_chunk = list(islice(input_iterator,buffer_size))
                    if filter:
                        current_chunk = self.filter_chunk(current_chunk)
                    if current_chunk:
                        current_chunk.sort(key=key)
                        output_chunk = file(os.path.join(tempdir,'%06i_%s_%s'%(len(chunks), os.getpid(), self.id)),'w+b',tempFileSize)
                        output_chunk.writelines(current_chunk)
                        output_chunk.flush()
                        output_chunk.seek(0)
                        chunks.append(output_chunk)
                    else:
                        break

            except KeyboardInterrupt: #If there is an interruption, delete all temporary files and raise the exception for further processing.
                print 'Removing temporary files...'
                for chunk in chunks:
                    try:
                        chunk.close()
                        os.remove(chunk.name)
                    except:
                        pass
                raise

            finally:
                input_file.close()
                
            if output is None:
                output = "%s/tempsort%s_%s"%(gettempdir(), os.getpid(), self.id)
            
            output_file = file(output,'wb',tempFileSize)
            
            try:
                output_file.writelines(self.merge(chunks,key))
            finally:
                for chunk in chunks:
                    try:
                        chunk.close()
                        os.remove(chunk.name)
                    except:
                        pass

            output_file.close()
            return file(output)

        def merge(self, chunks,key=None):
            if key is None:
                key = lambda x : x

            values = []
            for index, chunk in enumerate(chunks):
                try:
                    iterator = iter(chunk)
                    value = iterator.next()
                except StopIteration:
                    try:
                        chunk.close()
                        os.remove(chunk.name)
                        chunks.remove(chunk)
                    except:
                        pass
                else:
                    heappush(values,((key(value),index,value,iterator,chunk)))

            while values:
                k, index, value, iterator, chunk = heappop(values)
                yield value
                try:
                    value = iterator.next()
                except StopIteration:
                    try:
                        chunk.close()
                        os.remove(chunk.name)
                        chunks.remove(chunk)
                    except:
                        pass
                else:
                    heappush(values,(key(value),index,value,iterator,chunk))
                    
                    
class SortedFileClusterReader:
    """Holds a cursor and a file path. Given a start and an end, it iterates through the file starting on the cursor position,
    and retrieves the clusters that overlap with the region specified. Uses linecache.
    This class is thought for substituting the code in "def subtract" and "def remove_regions" inside picos.operations.
    """
    def __init__(self, file_path, read_format, read_half_open=False, rounding = True):
        self.__dict__.update(locals())
        self.slow_cursor = 1
        self.cluster_cache = dict() #TODO test if this actually improves speed (I think it does, but I could be wrong)
        self.invalid_count = 0
        self.invalid_limit = 40

    def _read_line_load_cache(self, cursor):
        """Loads the cache if the line read by the cursor is not there yet.
        If the line is empty, it means that the end of file was reached,
        so this function sends a signal for the parent function to halt """
        if cursor not in self.cluster_cache:
            line = linecache.getline(self.file_path, cursor)
            if line == '':
                return True
            self.cluster_cache[cursor] = Cluster(read=self.read_format, read_half_open=self.read_half_open, rounding=self.rounding)
            self.safe_read_line(self.cluster_cache[cursor], line)
        return False

    def get_overlaping_clusters(self, region, overlap=1):
        clusters = []
        if self._read_line_load_cache(self.slow_cursor):
            return clusters
        #advance slow cursor and delete the clusters that are already passed by
        while (self.cluster_cache[self.slow_cursor].chromosome < region.chromosome) or (self.cluster_cache[self.slow_cursor].chromosome == region.chromosome and region.start > self.cluster_cache[self.slow_cursor].end):
            del self.cluster_cache[self.slow_cursor]
            self.slow_cursor+=1
            if self._read_line_load_cache(self.slow_cursor):
                return clusters
        #get intersections
        fast_cursor = self.slow_cursor
        while self.cluster_cache[fast_cursor].start <= region.end and self.cluster_cache[fast_cursor].chromosome == region.chromosome:
            if self.cluster_cache[fast_cursor].overlap(region) >= overlap:
                clusters.append(self.cluster_cache[fast_cursor].copy_cluster())
            fast_cursor += 1
            if self._read_line_load_cache(fast_cursor):
                return clusters
        return clusters

    #TODO read_safe_line dentro de Cluster, o Utils...
    def safe_read_line(self, cluster, line):
        """Reads a line in a file safely catching the error for headers.
        Triggers OperationFailed exception if too many invalid lines are fed to the method"""
        try:
            cluster.read_line(line)
        except InvalidLine:
            if self.invalid_count > self.invalid_limit:
                print
                self.logger.error('Limit of invalid lines: Check the input, control, and annotation formats, probably the error is in there. Pyicos by default expects pk files, except for annotation files, witch are bed files')
                print
                raise OperationFailed
            else:
                print "Skipping invalid (%s) line: %s"%(cluster.reader.format, line),
                self.invalid_count += 1



class Turbomix:
    """
    This class is the pipeline that makes possible the different combination of operations. 
    It has different switches that are activated by the list 'operations'.
    """
    logger = logging.getLogger("log/picos.log")
    logger.setLevel(logging.WARNING)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    formatter = logging.Formatter("%(levelname)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    invalid_count = 0
    invalid_limit = 10
    control_path = None
    
    def __init__(self, input_path, output_path, read_format=BED, write_format=PK, experiment_name='picos_experiment', 
                 is_input_open=False, is_output_open=False, debug = False, rounding=False, tag_length = None, discarded_chromosomes = None,
                 control_path = None, control_format = PK, is_control_open = False, annotation_path = None, annotation_format = PK, 
                 is_annotation_open=False, span = 20, extension = 0, p_value = 0.05, height_limit = 20, correction_factor = 1, trim_percentage=0.15, no_sort=False,
                 tolerated_duplicates=sys.maxint, threshold=None, trim_absolute=None):
        self.__dict__.update(locals())
        self.is_sorted = False
        self.operations = []
        self.discarded_chromosomes = []
        if discarded_chromosomes:
            self.discarded_chromosomes = discarded_chromosomes.split(':')
        self.previous_chr = None
        if control_path is not None:
            self.fast_subtract_cursor = 1
            self.slow_subtract_cursor = 1
        self.annotation_cluster = Cluster(read=annotation_format, read_half_open = is_annotation_open)
        if annotation_path is not None:
            self.fast_annotation_cursor = 1
            self.slow_annotation_cursor = 1
        self.is_annotation_open = False
        #Clusters and preprocessors
        try:
            self.input_preprocessor = Utils.BigSort(read_format, is_input_open, extension, 'input')
            self.cluster = Cluster(read=self.read_format, write=self.write_format, rounding=self.rounding, read_half_open = self.is_input_open, write_half_open = self.is_output_open, tag_length=self.tag_length, span = self.span)
            self.cluster_aux = Cluster(read=self.read_format, write=self.write_format, rounding=self.rounding, read_half_open = self.is_input_open, write_half_open = self.is_output_open, tag_length=self.tag_length, span = self.span)
            self.cluster_duplicate = Cluster(read=self.read_format, write=self.write_format, rounding=self.rounding, read_half_open = self.is_input_open, write_half_open = self.is_output_open, tag_length=self.tag_length, span = self.span)
            self.control_cluster = Cluster(read=control_format, read_half_open = is_control_open)
            self.control_preprocessor = Utils.BigSort(control_format, is_control_open, extension, 'control')
        except ConversionNotSupported:
            print '\nThe reading "%s" and writing "%s" is not supported. \n\nFormats Pyicos can read:'%(self.read_format, self.write_format)
            for format in READ_FORMATS:
                print format
            print '\nFormats Pyicos can write:'
            for format in WRITE_FORMATS:
                print format
            sys.exit(0)
        #poisson stuff
        self.first_chr = True
        self._init_poisson()
        self.poisson_results = {'basepair': defaultdict(), 'clusterheight': defaultdict(), 'numreads': defaultdict() }
        self.maxheight_to_pvalue = {}
        #Operation flags
        self.do_poisson = False
        self.do_subtract = False
        self.do_heuremove = False
        self.do_split = False
        self.do_trim = False
        self.do_cut = False
        self.do_extend = False
        self.do_dupremove = False
        self.sorted_by_picos = False
        

    def i_cant_do(self):
        """Quits and exists if exits if the combination of non possible operations"""
        if Cut in self.operations and Poisson not in self.operations and not self.threshold:
            print 'cannot do Cut without Poisson or a fixed threshold\n'
            sys.exit(0)
        elif (Subtract in self.operations or Split in self.operations) and (self.write_format not in CLUSTER_FORMATS):
            print 'Cant get the output as tag format (eland, bed) for Subtract, Split or Callclusters commands, please use a cluster format (pk, wig...)\n'
            sys.exit(0)
        elif Extend in self.operations and self.read_format in CLUSTER_FORMATS:
            print '(Under development) Cant extend if the input is a clustered format ',
            print CLUSTER_FORMATS
            sys.exit(0)
    
    def _init_poisson(self):
        self.genome_start = sys.maxint        
        self.genome_end = 0
        self.total_bp_with_reads = 0.
        self.total_clusters = 0.
        self.total_reads = 0.
        self.acum_height = 0.
        self.absolute_max_height = 0
        self.absolute_max_numreads = 0
        self.heights =  defaultdict(int)
        self.max_heights =  defaultdict(int)
        self.numreads_dict =  defaultdict(int)
        
    def _add_slash_to_path(self, path):
        return Utils.add_slash_to_path(path)

    def read_and_preprocess(self, cluster, line):
        self.safe_read_line(cluster, line)
        if self.do_dupremove:
            cluster = self.remove_duplicates(cluster)
        if self.do_heuremove:
            cluster = self.remove_regions(cluster)
        if self.do_extend:
            cluster.extend(self.extension)
    
    def read_and_preprocess_no_heuremove(self, cluster, line):
        """For the control I dont need to remove the annotation regions, this way it will be faster"""
        self.safe_read_line(cluster, line)
        if self.do_dupremove:
            cluster = self.remove_duplicates(cluster)
        if self.do_extend:
            cluster.extend(self.extension)

    def success_message(self, output_path):
        print 'Success!\n'
        print 'Summary of operations:'
        self.result_log.write('\nSummary of operations:\n----------------\n')
        if self.read_format != self.write_format and not NoWrite in self.operations:
            print 'Convert from %s to %s.'%(self.read_format, self.write_format)

        if Extend in self.operations:
            self.result_log.write('Extend\n')
            print 'Extend'

        if self.do_subtract:
            self.result_log.write('Subtract\n')
            print 'Subtract'

        if self.discarded_chromosomes:
            self.result_log.write('Discard tags: %s\n'%self.discarded_chromosomes)
            print 'Discard tags: %s'%self.discarded_chromosomes

        if self.do_heuremove:
            self.result_log.write('Heuristic Remove from %s\n'%self.annotation_path)
            print 'Heuristic Remove from %s'%self.annotation_path

        if self.do_split:
            self.result_log.write('Split\n')
            print 'Split'

        if Extend in self.operations:
            self.result_log.write('Remove Artifacts\n')
            print 'Remove Artifacts'
            
        if self.do_poisson:
            self.result_log.write('Poisson\n')
            print 'Poisson'

        if self.do_cut:
            self.result_log.write('Cut\n')
            print 'Cut'


        self.result_log.write('Date finished: %s'%datetime.now())
        
        if not NoWrite in self.operations:
            print 'Output at: %s'%(output_path)

    def start_operation_message(self):
        if self.read_format != self.write_format:
            print 'Converting file %s from %s to %s...'%(self.current_input_path, self.read_format, self.write_format)
        else:
            print 'Reading file %s as a %s file...'%(self.current_input_path, self.read_format)

        if self.current_control_path:
            print 'Control file:%s'%self.current_control_path
            if Normalize in self.operations:
                print 'The file %s will be normalized to match %s'%(self.current_input_path, self.current_control_path)
            if Subtract in self.operations:
                print 'The file %s will be substracted from %s'%(self.current_control_path, self.current_input_path)

    def get_normalize_factor(self, input, control):
        ret = self.numcells(control, self.control_format)/self.numcells(input, self.read_format)
        self.result_log.write('Normalization factor: %s\n'%(ret))
        print 'Normalization factor: %s\n'%(ret)
        return ret
    
    def numcells(self, file_path, file_format):
        """Returns the total number of cells in a file"""
        num = 0.
        cluster = Cluster(read=file_format)
        for line in file(file_path, 'rb'):
            self.safe_read_line(cluster, line)
            for level in cluster: 
                num += level[0]*level[1]
            cluster.clear()

        return num  
    
    def safe_read_line(self, cluster, line):
        """Reads a line in a file safely catching the error for headers. 
        Triggers OperationFailed exception if too many invalid lines are fed to the method"""
        try:
            cluster.read_line(line)
        except InvalidLine:
            if self.invalid_count > self.invalid_limit:
                print
                self.logger.error('Limit of invalid lines: Check the input, control, and annotation formats, probably the error is in there. Pyicos by default expects pk files, except for annotation files, witch are bed files')
                print
                raise OperationFailed
            else:
                print "Skipping invalid (%s) line: %s"%(cluster.reader.format, line),
                self.invalid_count += 1

    def run(self):
        self.do_subtract = (Subtract in self.operations and self.control_path is not None)
        self.do_heuremove = (RemoveRegion in self.operations and self.annotation_path)
        self.do_poisson = Poisson in self.operations
        self.do_split = Split in self.operations
        self.do_trim = Trim in self.operations
        self.do_cut = Cut in self.operations
        self.do_extend = Extend in self.operations and not self.sorted_by_picos #If the input was sorted by Pyicos, it was already extended before, so dont do it again
        self.do_discard = DiscardArtifacts in self.operations
        self.do_dupremove = RemoveDuplicates in self.operations
        print
        print '\nPyicos running...'
        if not self.control_path:
            self.process_all_files_recursive(self.input_path, self.output_path)
        else:
            self.process_all_files_with_control()

    def process_all_files_with_control(self):
        if os.path.isdir(self.input_path):
            self.logger.warning('Operating with directories will only give appropiate results if the files and the control are paired in alphabetical order.')
            controls = os.listdir(self.control_path)
            controls.sort()
            experiments = os.listdir(self.input_path)
            experiments.sort()
            for i in xrange(0, len(experiments)):
                try:
                    operate(experiments[i], control[i], '%s%s'%(self.output_path, experiments[i]))
                except IndexError:
                    pass
        else:
            output = self.output_path
            if os.path.isdir(self.output_path):
                output = self._add_slash_to_path(self.output_path)
                output = '%s%s_minus_%s'%(self.output_path, os.path.basename(self.input_path), os.path.basename(self.control_path))
            
        self.operate(self.input_path, self.control_path, output)
    
    def process_all_files_recursive(self, dirorfile, output, firstIteration=True):
        paths = []
        """Goes through all the directories and creates files recursively. Returns the paths of the resulting files"""
        if os.path.isdir(dirorfile):
            dirorfile = self._add_slash_to_path(dirorfile)
            if not firstIteration:
                output = '%s/%s/'%(os.path.abspath(output), dirorfile.split('/')[-2])
                if not os.path.exists(output):
                    os.makedirs(output)  
            
            for filename in os.listdir(dirorfile):
                self.process_all_files_recursive('%s%s'%(dirorfile,filename), output)
                
        elif os.path.isfile(os.path.abspath(dirorfile)):
            if os.path.isdir(output):
                output = '%s%s.%s'%(self._add_slash_to_path(output), (os.path.basename(dirorfile)).split('.')[0], self.write_format)
            try:
                self.operate(input_path=os.path.abspath(dirorfile), output_path=os.path.abspath(output))
                paths.append(output)
            except OperationFailed:
                self.logger.warning('%s couldnt be read. Skipping to next file.'%os.path.abspath(dirorfile))
                os.remove(os.path.abspath(output))
            except StopIteration:
                self.logger.warning('%s End of file reached.'%os.path.abspath(dirorfile))
        else:
            self.logger.error('Input "%s" doesnt exist?'%os.path.abspath(dirorfile))

        return paths

    def normalize(self):
        if self.control_path:
            print 'Calculating normalization factor...'
            self.normalize_factor = self.get_normalize_factor(self.current_input_path, self.current_control_path)
            self.cluster.normalize_factor = self.normalize_factor
            self.cluster_aux.normalize_factor = self.normalize_factor

    def get_lambda_func(self, format):
        if self.write_format == SPK:
            if format == ELAND:
                return lambda x:(x.split()[6],x.split()[8],int(x.split()[7]),len(x.split()[1]))
            else:
                return lambda x:(x.split()[0],x.split()[5],int(x.split()[1]),int(x.split()[2]))
        else:
            if format == ELAND:
                return lambda x:(x.split()[6], int(x.split()[7]), len(x.split()[1]))
            else:
                return lambda x:(x.split()[0],int(x.split()[1]),int(x.split()[2]))

    def decide_sort(self, input_path, control_path=None):
        """Decide if the files need to be sorted or not."""
        if (not self.read_format in CLUSTER_FORMATS and self.write_format in CLUSTER_FORMATS) or self.do_subtract or self.do_heuremove or self.do_dupremove:
            if self.no_sort:
                print 'Input sort skipped'
                self.sorted_input_file = file(input_path)
                self.current_input_path = self.sorted_input_file.name
            else:
                print 'Sorting input file...'
                self.is_sorted = True
                self.sorted_input_file = self.input_preprocessor.sort(input_path, None, self.get_lambda_func(self.read_format), filter=(self.read_format == ELAND or Extend in self.operations))
                self.current_input_path = self.sorted_input_file.name

            if self.do_subtract:
                if self.no_sort:
                    print 'Control sort skipped'
                    self.sorted_control_file = file(control_path)
                    self.current_control_path = self.sorted_control_file.name
                else:
                    print 'Sorting control file...'
                    self.sorted_control_file = self.control_preprocessor.sort(control_path, None, self.get_lambda_func(self.control_format), filter=(self.control_format == ELAND or Extend in self.operations))
                    self.current_control_path = self.sorted_control_file.name

    def operate(self, input_path, control_path=None, output_path=None):
        """Operate expects single paths, not directories. Its called from run() several times if the input for picos is a directory"""
        try:
            self.i_cant_do()
            #per operation variables
            self.previous_chr = None
            self.current_input_path = input_path
            self.current_control_path = control_path
            self.current_output_path = output_path
            self.cluster.clear()
            self.cluster_aux.clear()
            self.result_log = file('%s/pyicos_report_%s.txt'%(os.path.dirname(os.path.abspath(self.output_path)), os.path.basename(self.current_input_path)), 'wb')
            self.result_log.write('PYICOS analysis report\n')
            self.result_log.write('----------------------\n\n')
            self.result_log.write('Date run: %s\n'%datetime.now())
            self.result_log.write('Input file: %s\n'%self.current_input_path)
            if self.current_control_path:
                self.result_log.write('Control file: %s\n'%self.current_control_path)
            self.result_log.write('\n\n')
            if self.write_format == WIG and self.is_output_open == False:
                self.logger.warning('You are creating a closed wig file. This will not be visible in the UCSC genome browser')

            self.start_operation_message()
            self.decide_sort(input_path, control_path)
            if Normalize in self.operations:
                self.normalize()

            if self.do_cut: #if we cut, we will round later
                self.cluster.rounding = False
                self.cluster_aux.rounding = False
       
            self.process_file()

            if self.do_poisson: #operate with the last chromosome and print the threadholds
                self.poisson_analysis(self.previous_chr)
                print '\nCluster threadsholds for p-value %s:'%self.p_value
                self.result_log.write('\nCluster threadsholds for p-value %s:\n'%self.p_value)
                for chromosome, k in self.poisson_results["clusterheight"].items():
                    print '%s: %s'%(chromosome, k)
                    self.result_log.write('%s: %s\n'%(chromosome, k))

            if self.do_cut: 
                self.cut()

            if ModFDR in self.operations:
              self.modfdr()

            self.success_message(output_path)

        finally: #Finally try deleting all temporary files, quit silently if they dont exist
            try:
                if not self.no_sort:
                    os.remove(self.sorted_input_file.name)
                    print 'Temporary file %s removed'%self.sorted_input_file.name
            except AttributeError, OSError:
                pass
            try:
                if not self.no_sort:
                    os.remove(self.sorted_control_file.name)
                    print 'Temporary file %s removed'%self.sorted_control_file.name
            except AttributeError, OSError:
                pass
 
    def _to_read_conversion(self, input, output):
        for line in input:
            try:
                self.read_and_preprocess(self.cluster, line)
                if not self.cluster.is_empty():
                    self.process_cluster(self.cluster, output)
                self.cluster.clear()
            except InsufficientData:
                self.logger.warning('For this type of conversion (%s to %s) you need to specify the tag length with the --tag-length flag'%(self.read_format, self.write_format))
                sys.exit(0)

    def _to_cluster_conversion(self, input, output):
        while self.cluster.is_empty():
            self.read_and_preprocess(self.cluster, input.next())

        for line in input:
            self.cluster_aux.clear()
            self.read_and_preprocess(self.cluster_aux, line)
            if self.cluster.intersects(self.cluster_aux) or self.cluster_aux.is_contiguous(self.cluster) or self.cluster.is_contiguous(self.cluster_aux):
                self.cluster += self.cluster_aux
            else:
                if not self.cluster.is_empty():
                    self.process_cluster(self.cluster, output)
                self.cluster.clear()
                self.read_and_preprocess(self.cluster, line)

        if not self.cluster.is_empty():
            self.process_cluster(self.cluster, output)
            self.cluster.clear()

    def process_file(self):
        if NoWrite in self.operations:
            output = None
        else:
            output = file(self.current_output_path, 'wb')
        input = file(self.current_input_path, 'rb')

        if self.write_format == WIG or self.write_format == VARIABLE_WIG:
            output.write('track type=wiggle_0\tname="%s"\tvisibility=full\n'%self.experiment_name)

        if self.write_format in CLUSTER_FORMATS:
            if not self.read_format in CLUSTER_FORMATS:
                print 'Clustering reads...'
            self._to_cluster_conversion(input, output)
        else:
            self._to_read_conversion(input, output)

        if not NoWrite in self.operations:
            output.flush()
            output.close()

    def subtract(self, cluster):
        self.control_cluster.clear()
        line = linecache.getline(self.current_control_path, self.slow_subtract_cursor)
        if line == '': return cluster
        self.read_and_preprocess_no_heuremove(self.control_cluster, line)
        #advance slow cursor
        while (self.control_cluster.chromosome < cluster.chromosome) or (self.control_cluster.chromosome == cluster.chromosome and cluster.start > self.control_cluster.end):
            self.control_cluster.clear()
            self.slow_subtract_cursor+=1
            line = linecache.getline(self.current_control_path, self.slow_subtract_cursor)
            if line == '': return cluster
            self.read_and_preprocess_no_heuremove(self.control_cluster, line)

        self.fast_subtract_cursor = self.slow_subtract_cursor
        while self.control_cluster.start <= cluster.end and self.control_cluster.chromosome == cluster.chromosome:
            if cluster.intersects(self.control_cluster):
                cluster -= self.control_cluster
            self.fast_subtract_cursor += 1
            self.control_cluster.clear()
            line = linecache.getline(self.current_control_path, self.fast_subtract_cursor)
            if line == '': return cluster
            self.read_and_preprocess_no_heuremove(self.control_cluster, line)

        return cluster

    def remove_regions(self, cluster):
        self.annotation_cluster.clear()
        line = linecache.getline(self.annotation_path, self.slow_annotation_cursor)
        if line == '': return cluster
        self.safe_read_line(self.annotation_cluster, line)
        #advance slow cursor
        while (self.annotation_cluster.chromosome < cluster.chromosome) or (self.annotation_cluster.chromosome == cluster.chromosome and cluster.start > self.annotation_cluster.end):
            self.annotation_cluster.clear()
            self.slow_annotation_cursor+=1
            line = linecache.getline(self.annotation_path, self.slow_annotation_cursor)
            if line == '': return cluster
            self.safe_read_line(self.annotation_cluster, line)

        self.fast_annotation_cursor = self.slow_annotation_cursor
        while self.annotation_cluster.start <= cluster.end  and self.annotation_cluster.chromosome == cluster.chromosome:
            if cluster.overlap(self.annotation_cluster) >= 0.5:
                cluster.clear()
                return cluster #Its discarded, so its over. Return the empty cluster.

            self.fast_annotation_cursor += 1
            self.annotation_cluster.clear()
            line = linecache.getline(self.annotation_path, self.fast_annotation_cursor)
            if line == '': return cluster
            self.safe_read_line(self.annotation_cluster, line)

        return cluster

    def remove_duplicates(self, cluster):
        """Removes the duplicates found in the input file (under development)"""
        if cluster == self.cluster_duplicate and not cluster.is_empty():
            self.identical_reads+=1
            if self.identical_reads > self.tolerated_duplicates:
                #print 'Duplicate number %s discarded: %s'%(self.duplicates_found, cluster.write_line())
                cluster.clear()
        else:
            self.cluster_duplicate = cluster.copy_cluster()
            self.identical_reads = 1

        return cluster

    def _correct_bias(self, p_value):
        if p_value < 0:
            return 0
        else:
            return p_value

    def poisson_analysis(self, chromosome=''):
        """
        We do 3 different global poisson statistical tests for each file:
        
        Nucleotide analysis:
        This analysis takes the nucleotide as the unit to analize. We give a p-value for each "height"
        of read per nucleotides using an accumulated poisson. With this test we can infer more accurately 
        what nucleotides in the cluster are part of the DNA binding site.
 
        Peak analysis:
        This analysis takes as a basic unit the "cluster" profile and performs a poisson taking into account the height
        of the profile. This will help us to better know witch clusters are statistically significant and which are product of chromatine noise

        We do this by calculating the acumulated maximum height for all existing clusters and dividing it by the number of clusters. This gives us the average height of a cluster.
        Given a mean and a   height k the poisson function gives us the probability p of one cluster having a height k by chance. With this if we want, for example,
        to know what is the probability of getting a cluster higher than k = 7, we accumulate the p-values for poisson(k"0..6", mean).
        
        Number of reads analysis:
        We analize the number of reads of the cluster. Number of reads = sum(xi *yi ) / read_length
        """
        if os.path.isdir(self.output_path):
           out = self.output_path
        else:
           out = os.path.dirname(os.path.abspath(self.output_path))
        self.result_log.write('---------------\n')
        self.result_log.write('%s\n'%(chromosome))
        self.result_log.write('---------------\n\n')
        self.result_log.write('Correction factor: %s\n\n'%(self.correction_factor))
        self.reads_per_bp =  self.total_bp_with_reads / (self.genome_end-self.genome_start)*self.correction_factor
        p_nucleotide = 1.
        p_cluster = 1.
        p_numreads = 1.
        k = 0
        self.result_log.write('k\tBasepair\tPeak_height\tNumreads\n')
        while ((self.absolute_max_numreads > k) or (self.absolute_max_height > k)) and k < self.height_limit:
            p_nucleotide -= Utils.poisson(k, self.reads_per_bp) #analisis nucleotide
            p_cluster -= Utils.poisson(k, self.acum_height/self.total_clusters) #analysis cluster
            p_numreads -= Utils.poisson(k, self.total_reads/self.total_clusters) #analysis numreads
            p_nucleotide = self._correct_bias(p_nucleotide)
            p_cluster = self._correct_bias(p_cluster)
            p_numreads = self._correct_bias(p_numreads)
            self.result_log.write('%s\t%.8f\t%.8f\t%.8f\n'%(k, p_nucleotide, p_cluster, p_numreads))
                
            if chromosome not in self.poisson_results['basepair'].keys() and p_nucleotide < self.p_value:
                self.poisson_results["basepair"][chromosome] = k

            if chromosome not in self.poisson_results['clusterheight'].keys() and p_cluster < self.p_value:
                self.poisson_results["clusterheight"][chromosome] = k
            
            if chromosome not in self.poisson_results['numreads'].keys() and p_numreads < self.p_value:
                self.poisson_results["numreads"][chromosome] = k

            if k not in self.maxheight_to_pvalue:
                self.maxheight_to_pvalue[k] = {}
            self.maxheight_to_pvalue[k][chromosome] = p_cluster
            k+=1

    def poisson_retrieve_data(self, cluster):
        acum_numreads = 0.
        self.total_clusters+=1
        self.genome_start = min(self.genome_start, cluster.start)
        self.genome_end = max(self.genome_end, cluster.end) 
        for length, height in cluster:
            self.heights[height] += length
            self.total_bp_with_reads+=length
            acum_numreads += length*height
            
        max_height = cluster.get_max_height()
        #numreads per cluster
        numreads_in_cluster = acum_numreads/self.extension
        self.total_reads += numreads_in_cluster
        self.absolute_max_numreads = max(numreads_in_cluster, self.absolute_max_numreads)
        self.numreads_dict[int(numreads_in_cluster)] += 1
        #maxheight per cluster
        self.max_heights[max_height] += 1
        self.acum_height += max_height
        self.absolute_max_height = max(max_height, self.absolute_max_height)

    def process_cluster(self, cluster, output):
        if cluster.chromosome not in self.discarded_chromosomes and not cluster.is_empty():
            if self.previous_chr != cluster.chromosome: #A new chromosome has been detected
                linecache.clearcache() #new chromosome, no need for the cache anymore
                if self.is_sorted:
                    print 'Processing %s...'%cluster.chromosome

                if self.do_poisson and not self.first_chr:
                    self.poisson_analysis(self.previous_chr)
                    self._init_poisson()
                self.previous_chr = cluster.chromosome
                
                if self.write_format == VARIABLE_WIG:
                    output.write('variableStep\tchrom=%s\tspan=%s\n'%(self.previous_chr, self.span))
                self.first_chr = False

            if self.do_subtract:
                cluster = self.subtract(cluster)
                for cluster in cluster.split(threshold=0):
                    self._post_subtract_process_cluster(cluster, output)
            else:
                self._post_subtract_process_cluster(cluster, output)

    def _post_subtract_process_cluster(self, cluster, output):
        if self.do_poisson:
            self.poisson_retrieve_data(cluster)

        if not (cluster.is_artifact() and DiscardArtifacts in self.operations) and not NoWrite in self.operations:
            if self.do_trim:
                cluster.trim(self.trim_absolute)

            if self.do_split:
                for subcluster in cluster.split(self.trim_percentage, self.trim_absolute):
                    self.extract_and_write(subcluster, output)
            else:
                self.extract_and_write(cluster, output)


    def extract_and_write(self, cluster, output):
        """The line will be written to the file if the last conditions are met"""
        if not cluster.is_empty() and cluster.start > -1:
            output.write(cluster.write_line())

    def _current_directory(self):
        return os.path.abspath(os.path.dirname(self.current_output_path))

    def cut(self):
        """Discards the clusters that dont go past the threadshold calculated by the poisson analysis"""
        current_directory = self._current_directory()
        old_output = '%s/deleteme_%s'%(current_directory, os.path.basename(self.current_output_path))
        shutil.move(os.path.abspath(self.current_output_path), old_output)
        real_output = file(self.current_output_path, 'w+')
        unfiltered_output = file('%s/unfiltered_%s'%(current_directory, os.path.basename(self.current_output_path)), 'w+')
        if self.write_format == WIG or self.write_format == VARIABLE_WIG:
            wig_header = 'track type=wiggle_0\tname="%s"\tvisibility=full\n'%self.experiment_name
            real_output.write(wig_header)
            unfiltered_output.write(wig_header)
        cut_cluster = Cluster(read=self.write_format, write=self.write_format, rounding=self.rounding, read_half_open = self.is_output_open, write_half_open = self.is_output_open, tag_length=self.tag_length, span = self.span)
        print 'Writing filtered and unfiltered file...'
        for line in file(old_output):
                cut_cluster.clear()
                self.safe_read_line(cut_cluster, line)
                try:
                    if self.threshold:
                        thres = self.threshold
                    else:
                        thres = self.poisson_results["clusterheight"][cut_cluster.chromosome]
                        
                    if cut_cluster.is_significant(thres):
                        real_output.write(cut_cluster.write_line())
                except KeyError:
                    real_output.write(cut_cluster.write_line()) #If its not in the dictionary its just too big and there was no pvalue calculated, just write it
                #write to the unfiltered file
                if not cut_cluster.is_empty():
                    try:
                        unfiltered_output.write('%s\t%.8f\n'%(cut_cluster.write_line().strip(), self.maxheight_to_pvalue[int(round(cut_cluster.get_max_height()))][cut_cluster.chromosome]))
                    except KeyError:
                        unfiltered_output.write('%s\t0.00000000\n'%cut_cluster.write_line().strip()) #If the cluster is not in the dictionary, it means its too big, so the p_value will be 0

        os.remove(old_output)


    def modfdr(self):
        old_output = '%s/deleteme_%s'%(self._current_directory(), os.path.basename(self.current_output_path))
        cluster_reader = SortedFileClusterReader(old_output, self.write_format)
        shutil.move(os.path.abspath(self.current_output_path), old_output)
        real_output = file(self.current_output_path, 'w+')
        unfiltered_output = file('%s/unfiltered_%s'%(self._current_directory(), os.path.basename(self.current_output_path)), 'w+')
        for region_line in file(self.annotation_path):
            print region_line
            split_region_line = region_line.split()
            region = Region(split_region_line[1], split_region_line[2], chromosome=split_region_line[0])
            overlaping_clusters = cluster_reader.get_overlaping_clusters(region, overlap=0.000001)
            print overlaping_clusters
            for cluster in overlaping_clusters:
                print cluster.write_line()
                unfiltered_output.write(cluster.write_line())
            region.add_tags(overlaping_clusters)
            for cluster in region.get_FDR_clusters():
                real_output.write(cluster.write_line())


    def strand_correlation(self):
        positive_cluster = None
        negative_cluster = None
        self.analized_pairs = 0.
        for line in sorted_pk:
            cluster = Cluster(line, rounding = True)
            if (cluster.get_max_height() > self.height_filter) and not cluster.has_duplicates(self.duplicate_limit):
                    if cluster.strand == '+':
                        positive_cluster = copy.deepcopy(cluster)#big positive cluster found
                        self._start_analysis(positive_cluster, negative_cluster)
                    else:
                        negative_cluster = copy.deepcopy(cluster)#big negative cluster found
                        self._start_analysis(positive_cluster, negative_cluster)

        print 'FINAL DELTAS:'
        data = []
        for delta in range(self.min_delta, self.max_delta, self.delta_step):
            if delta in self.delta_results:
                self.delta_results[delta]=self.delta_results[delta]/self.analized_pairs
                data.append(self.delta_results[delta])
                self.log.write_line('Delta %s:%s'%(delta, self.delta_results[delta]))

        try:
            import matplotlib.pyplot
            matplotlib.pyplot.plot(range(self.min_delta, self.max_delta), data)
            matplotlib.pyplot.plot()
            matplotlib.pyplot.savefig('%s%s.png'%(self.output_dir, os.path.basename(file_path)))
            #matplotlib.pyplot.show()
        except ImportError:
            print 'you dont have matplotlib installed, therefore picos cant create the graphs'
        except:
            print 'cant print the plots, unknown error'


    def _start_analysis(self, positive_cluster, negative_cluster):
        if positive_cluster is not None and negative_cluster is not None:
            if (abs(negative_cluster.start-positive_cluster.end) < self.max_delta or abs(positive_cluster.start-negative_cluster.end) < self.max_delta or positive_cluster.intersects(negative_cluster)) and positive_cluster.chr == negative_cluster.chr:
                self.analized_pairs+=1
                print 'Pair of clusters:'
                print positive_cluster.line(), negative_cluster.line(),
                for delta in range(self.min_delta, self.max_delta+1, self.delta_step):
                    r_squared = self.analize_paired_clusters(positive_cluster, negative_cluster, delta)[0]**2
                    if delta not in self.delta_results:
                        self.delta_results[delta] = r_squared
                    else:
                        self.delta_results[delta] += r_squared
                    #print 'Delta %s:%s'%(delta, result)






