"""
Class ICEIterative for iterative clustering and error correction.
"""
import cPickle
import math
import os
import os.path as op
import shutil
import logging
import random
import time
from pbcore.io.FastaIO import FastaReader
from multiprocessing.pool import ThreadPool
from datetime import datetime
from pbtools.pbtranscript.Utils import mknewdir, real_upath
from pbtools.pbtranscript.io.FastaRandomReader import FastaRandomReader
from pbtools.pbtranscript.io.BLASRRecord import BLASRM5Reader
from pbtools.pbtranscript.ice.ProbModel import ProbFromQV, ProbFromFastq
from pbtools.pbtranscript.ice.IceInit import IceInit
from pbtools.pbtranscript.ice.IceUtils import sanity_check_gcon, \
    sanity_check_sge, possible_merge, blasr_against_ref, \
    get_the_only_fasta_record, cid_with_annotation
from pbtools.pbtranscript.ice.IceFiles import IceFiles
from pbtools.pbtranscript.ice_pbdagcon import runConsensus
from pbtools.pbtranscript.ice.IceUtils import ice_fa2fq

random.seed(0)


class IceIterative(IceFiles):

    """
    Expects that new fasta files will be gradually added
    """

    def __init__(self, fasta_filename,
                 fasta_filenames_to_add,
                 all_fasta_filename,
                 ccs_fofn, root_dir,
                 ice_opts, sge_opts,
                 uc=None, probQV=None,
                 refs=None, d=None, is_FL=True, qv_prob_threshold=.1, fastq_filename=None,
                 use_ccs_qv=False):
        """
        fasta_filename --- the current fasta filename containing
            all the "active" reads (reads that are allowed to move
            to new clusters).
            usually called 'in.fa_split.0.fa' in the first round
            and 'current.fasta' after 1 round

        fastq_filename --- should be the FASTQ version of fasta_filename, if not given,
                           will be converted using ice_fa2fq

        use_ccs_qv --- by default, the FASTQ (single QV) will be used unless this is set to
                       True; another way around is to provide the probQV from reading .ccs.h5

        fasta_filenames_to_add --- fasta files to be added
            to clusters. In the first round, the union of
            fasta_filename and fasta_filenames to add is
            all_fasta_filename

        all_fasta_filename --- this should be the *entire* fasta,
            not the one that we'll start with.
        ccs_fofn --- should be reads_of_insert.fofn

        uc --- the initial or current cluster assignment,
            dict of cid --> list of members
        refs ---  dict of cid --> fasta file containing the cluster
            consensus fasta
        d  --- dict of read id --> dict of cid:prob

        qv_prob_threshold --- params for isoform accept/reject hit
        ice_opts --- params for isoform accept/reject, including
            ece_penalty, ece_min_len, maxScore
        probQV --- ProbFromQV object, this is what takes up all the memory

        sge_opts --- params for SGE environment, including
            use_sge     : use SGE or not
            max_sge_jobs : maximum number of gcon jobs submitted
            unique_id   : unique qsub job id, important that this
                         DOES NOT CONFLICT!
            blasr_nproc : blasr -nproc param, number of threads per cpu.
        """
        super(IceIterative, self).__init__(prog_name="IceIterative",
                                           root_dir=root_dir, ccs_fofn=ccs_fofn)

        self.fasta_filename = fasta_filename
        self.fasta_filenames_to_add = fasta_filenames_to_add
        self.all_fasta_filename = all_fasta_filename

        # The number of iterations (i.e. run_gcon_parallel_helper is called.)
        self.iterNum = 0

        # Sanity check gcon and sge
        self.gcon_py = sanity_check_gcon()
        self.use_sge = sge_opts.use_sge
        if self.use_sge:
            sanity_check_sge(self.script_dir)

        self.newids = set([r.name.split()[0]
                           for r in FastaReader(fasta_filename)])
        self.seq_dict = FastaRandomReader(all_fasta_filename)

        # probability dict, seqid --> cluster index i --> P(seq|C_i)
        self.d = {}

        self.refs = {}  # cluster index --> gcon output consensus filename
        self.uc = {}  # cluster index --> list of member seqids

        # list of cids that need to have gcon run (or re-run)
        self.changes = set()

        # size below which gcon must be re-run if changes were made
        self.rerun_gcon_size = 20

        self.is_FL = is_FL

        self.sge_opts = sge_opts
        self.num_jobs = sge_opts.max_sge_jobs
        self.blasr_nproc = sge_opts.blasr_nproc

        self.ice_opts = ice_opts
        self.qv_prob_threshold = qv_prob_threshold
        self.ece_penalty = ice_opts.ece_penalty
        self.ece_min_len = ice_opts.ece_min_len
        self.maxScore = ice_opts.maxScore

        self.unrun_cids = []

        # random prob of putting a singleton into another cluster
        self.random_prob = 0.3

        # unless fastq_filename is already given
        # converting input fasta + ccs_fofn --> fastq
        # so instead of using .ccs.h5 we can use just one QV from FASTQ
        if fastq_filename is not None:
            self.fastq_filename = fastq_filename
        else:
            self.fastq_filename = self.fasta_filename[:self.fasta_filename.rfind('.')] + '.fastq'
            self.add_log("Converting input fasta + ccs_fofn --> fastq.",
                        level=logging.INFO)
            ice_fa2fq(self.fasta_filename, ccs_fofn, self.fastq_filename)

        self.use_ccs_qv = use_ccs_qv
        if probQV is not None:
            self.add_log("Loading probabilities and QVs from given probQV.",
                         level=logging.INFO)
            self.probQV = probQV
        else:
            start_t = time.time()
            if self.use_ccs_qv:
                self.probQV = ProbFromQV(ccs_fofn, self.fasta_filename)
                self.add_log("Loading QV from ccs_fofn took {0} sec.".format(\
                            time.time()-start_t),\
                            level=logging.INFO)
            else:
                self.probQV = ProbFromFastq(self.fastq_filename)
                self.add_log("Loading QV from converted FASTQ file took {0} sec.".format(\
                            time.time()-start_t),\
                            level=logging.INFO)

        if uc is not None:
            self.add_log("Loading initial clusters from uc.",
                         level=logging.INFO)
            self.uc = uc
        else:
            errMsg = "IceInit.init_cluster_by_clique() should have been called."
            self.add_log(errMsg, level=logging.ERROR)
            raise RuntimeError(errMsg)

        # only runs things in 'uc', so safe to run in init
        if refs is None:
            self.add_log("Creating a reference dict using run_gcon_parallel.",
                         level=logging.INFO)
            self.run_gcon_parallel(self.uc.keys())
        else:
            self.add_log("loading references from a reference dict directly.",
                         level=logging.INFO)
            self.refs = refs

        # probability dict, seqid --> cluster index i --> P(seq|C_i)
        if d is None:
            self.add_log("Creating a prob dict using calc_cluster_prob.",
                         level=logging.INFO)
            self.init_d()
            self.calc_cluster_prob(force_calc=True)
        else:
            self.add_log("Loading probabilities from a prob dict directly.",
                         level=logging.INFO)
            self.d = d

        self.removed_qids = set()
        self.global_count = 0

    @property
    def tmpConsensusFa(self):
        """Return tmp consensus Fasta file. e.g.,
           output/tmp.consensus.fa
        """
        return op.join(self.out_dir, "tmp.consensus.fa")

    @property
    def tmpPickleFN(self):
        """Return file name of tmp pickle file. e.g.,
           output/tmp.pickle
        """
        return op.join(self.out_dir, "tmp.pickle")

    @property
    def currentFa(self):
        """Return the current fasta file."""
        return op.join(self.root_dir, "current.fasta")

    @property
    def refConsensusFa(self):
        """Return ref_consensus.fa"""
        return op.join(self.root_dir, "ref_consensus.fa")

    @property
    def tmpOrphanFa(self):
        """Return $root_dir/tmp.orphan.fa, a Fasta file of orphan sequences."""
        return op.join(self.root_dir, "tmp.orphan.fa")

    def gconDoneJobFN(self, iterNum):
        """Return scripts/gcon_done.sh"""
        return op.join(self.script_dir, str(iterNum), "gcon_done.sh")

    def alohaFN(self, iterNum):
        """Return scripts/aloha"""
        return op.join(self.script_dir, str(iterNum), "aloha")

    def uptoPickleFN(self, fa):
        """Given a Fasta file path/*.fa, return
           $self.out_dir/upto_*.fa.pickle
        """
        fn = fa.split('/')[-1]
        return op.join(self.out_dir, fn + ".pickle")

    def uptoConsensusFa(self, fa):
        """Given a Fasta file: path_to_fa/*.fa, return
           $self.out_dir/upto_*.fa.consensus.fa
        """
        fn = fa.split('/')[-1]
        return op.join(self.out_dir, fn + ".consensus.fa")

    def orphanFa(self, prefix):
        """Return $prefix.orphan.fa """
        return str(prefix) + ".orphan.fa"

    def selfBlasrFN(self, fn):
        """Return fn + ".self.blasr",
        e.g., tmp.orphan.fa.self.blasr"
        """
        return fn + ".self.blasr"

    def clusterInFa(self, cid):
        """Return in.fa for cluster i under
           $tmp_dir/($i/10000)/$i/in.fa
        """
        return op.join(self.cluster_dir(cid), "in.fa")

    def init_d(self):
        """Initialize the probability dictionary: seqid --> {}
        """
        for _cid, v in self.uc.iteritems():
            for qid in v:
                self.d[qid] = {}

    def sanity_check_uc_refs(self):
        """
        Check that all folders/files in refs exists
        For every cid in uc, there should be an entry in self.refs[cid]
        And there should be .tmp/<per-10k>/c<cid>/in.fa and the refs file
        """
        if self.refs is None:
            return
        for cid in self.uc:
            assert cid in self.refs and op.exists(self.refs[cid])

    @staticmethod
    def from_pickle(pickle_filename, probQV):
        """Load an instance of IceIterative from a pickle file."""
        a = None
        with open(pickle_filename) as h:
            a = cPickle.load(h)
        all_fasta_filename = a['all_fasta_filename']
        # need to make current.fasta!!!
        newids = a['newids']
        with open(a['fasta_filename'], 'w') as f:
            # for r in SeqIO.parse(open(all_fasta_filename), 'fasta'):
            for r in FastaReader(all_fasta_filename):
                rid = r.name.split()[0]
                if rid in newids:
                    f.write(">{0}\n{1}\n".format(rid, r.sequence))

        obj = IceIterative(
            fasta_filename=a['fasta_filename'],
            fasta_filenames_to_add=a['fasta_filenames_to_add'],
            all_fasta_filename=all_fasta_filename,
            ccs_fofn=a['ccs_fofn'],
            root_dir=a['root_dir'],
            ice_opts=a['ice_opts'],
            sge_opts=a['sge_opts'],
            uc=a['uc'],
            probQV=probQV,
            refs=a['refs'],
            d=a['d'],
            qv_prob_threshold=a['qv_prob_threshold'])
        obj.changes = a['changes']
        return obj

    def write_final_consensus(self):
        """
        Write output to final.consensus.fa
        Write output to final.consensus.fa.sa
        """
        msg = "Writing final consensus isoforms to " + \
              "{f}, ".format(f=self.final_consensus_fa) + \
              "and writing its suffix array to {sa}.".\
              format(sa=self.final_consensus_sa)
        self.add_log(msg, level=logging.INFO)

        self.write_consensus(fasta_filename=self.final_consensus_fa,
                             sa_filename=self.final_consensus_sa)

    def write_consensus(self, fasta_filename, sa_filename=None):
        """
        Write output to fasta_file
        Sequence ID format
        >c<cid>/abundance/length
        """
        with open(fasta_filename, 'w') as f:
            for cid, filename in self.refs.iteritems():
                assert cid in self.uc
                #r = SeqIO.read(open(filename), 'fasta')
                rs = [x for x in FastaReader(filename)]
                assert(len(rs) == 1)
                r = rs[0]
                newid = "c{cid}/{ab}/{le}".format(cid=cid,
                                                  ab=len(self.uc[cid]),
                                                  le=len(r.sequence))
                f.write(">{0}\n{1}\n".format(cid_with_annotation(newid),
                                             r.sequence))

        if sa_filename is not None:
            cmd = "sawriter {sa} {fa} -blt 8 -welter".\
                  format(fa=real_upath(fasta_filename),
                         sa=real_upath(sa_filename))
            self.add_log("Creating a suffix array for {f}.".
                         format(f=fasta_filename), level=logging.INFO)
            description = "Failed to create suffix array."
            self.run_cmd_and_log(cmd=cmd, description=description)

    def write_final_pickle(self):
        """Write the final pickle file."""
        msg = "Writing final pickle to {f}".format(
            f=self.final_pickle_fn)
        self.add_log(msg, level=logging.INFO)
        self.write_pickle(self.final_pickle_fn)

    def write_pickle(self, pickle_filename):
        """Write an instance of IceIterative to a pickle file."""
        with open(pickle_filename, 'w') as f:
            d = {'uc': self.uc,
                 'd': self.d,
                 'refs': self.refs,
                 'ccs_fofn': self.ccs_fofn,
                 'fasta_filename': self.fasta_filename,
                 'fasta_filenames_to_add': self.fasta_filenames_to_add,
                 'all_fasta_filename': self.all_fasta_filename,
                 'ice_opts': self.ice_opts,
                 'sge_opts': self.sge_opts,
                 'root_dir': self.root_dir,
                 'newids': self.newids,
                 'changes': self.changes,
                 'qv_prob_threshold': self.qv_prob_threshold
                 }
            cPickle.dump(d, f)

    def make_new_cluster(self):
        """Add a new cluster to self.uc."""
        best_i = max(self.uc.keys()) + 1
        self.uc[best_i] = []
        return best_i

    def remove_from_cluster(self, qID, from_i):
        """
        Remove a read (qID) from a cluster from_i,
        delete this cluster if it is empty.
        """
        self.uc[from_i].remove(qID)
        self.changes.add(from_i)
        if len(self.uc[from_i]) == 0:
            self.delete_cluster(from_i)

    def delete_cluster(self, from_i):
        """
        1) delete it from self.uc
        2) delete all related entries from self.d
        3) delete self.refs
        4) remove the directory
        5) remove from self.changes (if there)
        """
        del self.uc[from_i]
        for k in self.d:
            if from_i in self.d[k]:
                del self.d[k][from_i]
        del self.refs[from_i]

        dirname = self.cluster_dir(from_i)
        #op.join(self.tmp_dir, str(from_i/10000), 'c'+str(from_i))

        shutil.rmtree(dirname)
        if from_i in self.changes:
            self.changes.remove(from_i)

    def no_moves_possible(self):
        """
        Check self.uc, return True if no moves are possible, False otherwise."""
        for cid, members in self.uc.iteritems():
            if len(members) <= 2:  # match singleton criterion here
                for x in members:
                    if len(self.d[x]) > 1:
                        return False
            else:
                for x in members:
                    if cid not in self.d[x]:
                        return False
                    if self.d[x][cid] != max(self.d[x].itervalues()):
                        return False
        return True

    def run_gcon_parallel(self, cids):
        """
        A wrapper for run_gcon_parallel_helper

        In case some gcon jobs fail (qsub failure), it should pick it up
        and re-run them. If a job failed for > 50 times, abort!
        """
        self.unrun_cids = []
        self.run_gcon_parallel_helper(cids)
        # TODO: handle failed qsub jobs separately
        while len(self.unrun_cids) > 0 and self.iterNum <= 100:
            msg = "NEED to run gcon for {0} clusters: {1}.".\
                format(len(self.unrun_cids), ",".format(self.unrun_cids))
            self.add_log(msg)
            _cids = list(self.unrun_cids)
            self.unrun_cids = []
            self.run_gcon_parallel_helper(_cids)

    def gconJobFN(self, iterNum, gid):
        """Return file name of gcon job with in the iterNum-th iteration,
        while id=gid, e.g. scripts/$iterNum/gcon_job_1.sh
        """
        return op.join(self.script_dir,
                       str(iterNum),
                       "gcon_job_{gid}.sh".format(gid=gid))

    def elogFN(self, iterNum, scriptFN):
        """return elog for scriptFN, e.g., log/0/script.elog"""
        return op.join(self.log_dir, str(iterNum),
                       op.basename(scriptFN) + str(".elog"))

    def ologFN(self, iterNum, scriptFN):
        """return elog for scriptFN, e.g., log/0/script.olog"""
        return op.join(self.log_dir, str(iterNum),
                       op.basename(scriptFN) + str(".elog"))

    def run_gcon_parallel_helper(self, cids):
        """
        Run gcon on all clusters in <cids>
        Parallelize gcon calls to <self.num_jobs> nodes

        For each cid in <cids>,
        (1) ./tmp/c<cid>/in.fa is created
        (2) run gcon on <cid> only if the size > 2
        """
        # calculate effective chunk size
        #effective_cids = filter(lambda cid: len(self.uc[cid])>2, cids)

        # Create $root_dir/scripts/$iterNum/, e.g, clusterOut/scripts/0
        mknewdir(op.join(self.script_dir, str(self.iterNum)))

        # Create $root_dir/olog/$iterNum/ (e.g. clusterOut/log/0/)
        mknewdir(op.join(self.log_dir, str(self.iterNum)))

        effective_cids = [cid for cid in cids if len(self.uc[cid]) > 2]
        count_jobs = len(effective_cids)
        chunk_size = count_jobs / self.num_jobs + (count_jobs % self.num_jobs > 0)

        job_sh_dict = {}  # job_i --> file descriptor
        if count_jobs > 0:
            # ToDo: distribute jobs evenly (must look ahead at cluster sizes)
            numgconJobs = int(math.ceil(count_jobs / float(chunk_size)))
            #(count_jobs/chunk_size) + (count_jobs%chunk_size>0)
            for job_i in xrange(numgconJobs):
                f = open(self.gconJobFN(self.iterNum, job_i), 'w')
                # e.g. "scripts/$iterNum/gcon_job_{0}.sh"
                f.write("#!/bin/bash\n")
                job_sh_dict[job_i] = f

        effective_i = 0
        jobs = []
        for cid in cids:
            dirname = self.cluster_dir(cid)
            if op.exists(dirname):
                shutil.rmtree(dirname)
            os.makedirs(dirname)
            in_fa_filename = self.write_in_fasta(cid)

            if len(self.uc[cid]) <= 2:  # don't even bother running gcon
                # for now do nothing and let the else statement below
                # take care of it
                pass
            else:
                job_i = effective_i / chunk_size
                argstr = " {infa} ".format(infa=real_upath(in_fa_filename)) + \
                         " {cdir}/g_consensus".\
                         format(cdir=real_upath(self.cluster_dir(cid))) + \
                         " c{i}".format(i=cid) + \
                         " --nproc {nproc}".\
                         format(nproc=self.sge_opts.gcon_nproc) + \
                         " --maxScore {s}\n".format(s=self.maxScore)

                cmd = "{script} ".format(script=self.gcon_py) + argstr
                job_sh_dict[job_i].write(cmd)
                jobs.append(argstr)
                effective_i += 1

        if effective_i > 0:
            self.add_log("use_sge = {0}".format(self.use_sge))
            if self.use_sge is True:
                job_list = ''
                for job_i, f in job_sh_dict.iteritems():
                    f.close()
                    jid = "ice_iterative_{unique_id}_{it}_{j}".format(
                        unique_id=self.sge_opts.unique_id,
                        it=self.iterNum, j=job_i)
                    elog = self.elogFN(self.iterNum, f.name)
                    olog = self.ologFN(self.iterNum, f.name)

                    cmd = "qsub -pe smp {nproc} ".\
                        format(nproc=self.sge_opts.gcon_nproc) + \
                          " -S /bin/bash -V -cwd " + \
                          "-e {elog} ".format(elog=real_upath(elog)) + \
                          "-o {olog} ".format(olog=real_upath(olog)) + \
                          "-N {jid} ".format(jid=jid) + \
                          "{fn}".format(fn=real_upath(f.name))
                    self.qsub_cmd_and_log(cmd)
                    job_list += jid + ','

                with open(self.gconDoneJobFN(self.iterNum), 'w') as f:
                    f.write("touch {alohaFN}\n".format(
                        alohaFN=real_upath(self.alohaFN(self.iterNum))))
                msg = "waiting for gcon jobs to finish"
                self.add_log(msg)
                elog = self.elogFN(self.iterNum,
                                   self.gconDoneJobFN(self.iterNum))
                olog = self.ologFN(self.iterNum,
                                   self.gconDoneJobFN(self.iterNum))
                cmd = "qsub -sync y -pe smp 1 -S /bin/bash -V -cwd " + \
                      "-e {elog} ".format(elog=real_upath(elog)) + \
                      "-o {olog} ".format(olog=real_upath(olog)) + \
                      "-hold_jid {jl} ".format(jl=job_list[:-1]) + \
                      "-N qcon_done_{r} ".format(r=self.sge_opts.unique_id) + \
                      "{donesh}".format(donesh=real_upath(self.gconDoneJobFN(
                                        self.iterNum)))
                self.qsub_cmd_and_log(cmd)
            else:
                for job_i, f in job_sh_dict.iteritems():
                    f.close()

                msg = "Adding {n} threads for ice_pbdagcon jobs".\
                    format(n=len(jobs))
                self.add_log(msg, level=logging.INFO)
                time_1 = datetime.now()
                for job in jobs:
                    msg = "Adding a ice_pbdagcon job thread: " + \
                          self.gcon_py + job
                    self.add_log(msg)

                ts = []
                if len(jobs) > 0:
                    # Python's multiprocessing module copies all memory from
                    # the parent process.  In this case, it's likely due to all
                    # the QV values loaded into the main process thread
                    # that's getting replicated to the forked processes,
                    # causing memory bloat in all the children.

                    # Alternative ways are to use
                    #     threading.Thread or
                    #     multiprocessing.pool.ThreadPool
                    pool = ThreadPool(processes=self.sge_opts.gcon_nproc)
                    rets = pool.map(runConsensus, jobs)
                    pool.close()
                    pool.join()
                    # Check whether all threads finished successfully
                    for i, job in enumerate(jobs):
                        if rets[i] != 0:
                            errMsg = "CMD failed: {j}".format(j=job)
                            self.add_log(errMsg, level=logging.ERROR)
                            raise RuntimeError(errMsg)
                time_2 = datetime.now()
                msg = "Total time for {n} pbdagcon jobs is {t}.".\
                      format(n=len(jobs), t=time_2 - time_1)
                self.add_log(msg, level=logging.INFO)

        time_1 = datetime.now()
        msg = "Choosing ref files for {n} clusters.".format(n=len(cids))
        self.add_log(msg, level=logging.INFO)
        for cid in cids:
            self.refs[cid] = self.choose_ref_file(cid)
            msg = "Choosing ref file for {cid} = {f}".format(
                cid=cid, f=self.refs[cid])
            self.add_log(msg)

        time_2 = datetime.now()
        msg = "Total time for choosing {n} ref files is {t}.".\
            format(n=len(cids), t=time_2 - time_1)
        self.add_log(msg, level=logging.INFO)

        self.iterNum += 1

    def choose_ref_file(self, cid):
        """
        Return g_consensus.fa if not empty (i.e. gcon succeeded)
        Otherwise return g_consensus_ref.fa if not empty
        Finally, just randomly pick the 1st sequence as cluster representative
        """
        cons = self.g_consensus_fa_of_cluster(cid)
        cons_ref = self.g_consensus_ref_fa_of_cluster(cid)

        if op.exists(cons) and os.stat(cons).st_size > 0:
            # len(SeqIO.read(open(cons),'fasta')) > 0:
            self.add_log("Picking up {f} as reference.".format(f=cons))
            return cons
        elif op.exists(cons_ref) and os.stat(cons_ref).st_size > 0:
            # len(SeqIO.read(open(cons_ref),'fasta')) > 0:
            self.add_log("Picking up {f} as reference.".format(f=cons_ref))
            return cons_ref
        elif len(self.uc[cid]) > 3:
            self.add_log("Neither {0} nor {1} exists!.".format(cons, cons_ref))
            # should have run but did not, count as must re-run!!!
            self.add_log("{cid} has {n} reads, but no consensus, must rerun.".
                         format(cid=cid, n=len(self.uc[cid])))
            self.unrun_cids.append(cid)
            return None
        else:  # pick the 1st sequence
            self.add_log("Picking up the very first sequence as reference.")
            rname = self.first_seq_fa_of_cluster(cid)
            with open(rname, 'w') as h:
                h.write(">c{0}\n{1}\n".
                        format(cid, self.seq_dict[self.uc[cid][0]].sequence))
            return rname

    def clean_prob_for_cids(self, cids):
        """
        Takes |qIDs| x |cIds| time....

        For every d[qID][cID] such that qID is in self.newids
        and cID is in cids, delete it
        """
        for qid in self.newids:
            # msg = "cleaning prob for {qid}".format(qid=qid)
            # self.add_log(msg)
            for cid in set(cids).intersection(self.d[qid]):
                del self.d[qid][cid]

    def final_round_before_freeze(self, min_cluster_size):
        """
        Only do 1 kind of move:
           for any qID where self.d[qID][cID] is not the best one
           remove it and put it in the next batch, i.e.
        --- remove it from self.uc (delete the cluster if it becomes empty)
        --- delete entry self.d[qID]

        then, run gcon on the changed clusters

        ToDo: to do this iteratively would require actively removing qids
              from self.fasta_filename, otherwise self.calc_cluster_prob()
              will break. For now, just run once.
        """
        self.removed_qids = set()
        self.changes = set()
        cids = self.uc.keys()

        for cid in cids:
            n = len(self.uc[cid])
            for qid in self.uc[cid]:
                if (n < min_cluster_size or
                        cid not in self.d[qid] or
                        self.d[qid][cid] != max(self.d[qid].itervalues())):
                    msg = "Final round: remove {0} (from {1}) because {2}".\
                        format(qid, cid, self.d[qid])
                    self.add_log(msg)
                    del self.d[qid]
                    self.remove_from_cluster(qid, cid)
                    if (cid in self.uc and
                            len(self.uc[cid]) < self.rerun_gcon_size):
                        self.changes.add(cid)
                    self.removed_qids.add(qid)
        self.run_gcon_parallel(self.changes)

    def write_in_fasta(self, cid):
        """
        Write the ./tmp/<cid/10000 mod>/c<cid>/in.fa for cluster cid.
        """
        #in_filename = op.join('./tmp/', str(cid/10000), 'c'+str(cid), 'in.fa')
        in_filename = op.join(self.clusterInFa(cid))
        with open(in_filename, 'w') as f:
            for seqid in self.uc[cid]:
                f.write(">{0}\n{1}\n".format(seqid,
                                             self.seq_dict[seqid].sequence))
        return in_filename

    def add_seq_to_cluster(self):
        """
        After running blasr of newids (current batch) against existing
        clusters,
        (1) for all ids that have a non-empty prob dict, add to the best
            cluster
        (2) otherwise, put in orphan group (to run clique finding later)
        """
        orphan = []
        for sid in self.newids:
            if len(self.d[sid]) == 0:  # no match to existing cluster
                orphan.append(sid)
            else:
                best = self.d[sid].items()
                best.sort(key=lambda x: x[1], reverse=True)
                cid = best[0][0]
                r = self.seq_dict[sid]
                msg = "adding {0} to c{1}".format(sid, cid)
                self.add_log(msg)
                if len(self.uc[cid]) < self.rerun_gcon_size:
                    self.write_in_fasta(cid)
                    # I don't think this is really needed since when
                    # run_gcon_parallel is called later it regenerates
                    # the in.fa along with the whole folder
                    self.changes.add(cid)
                self.uc[cid].append(r.name.split()[0])
        return orphan

    def add_uc(self, uc):
        """
        Add new clusters (probably after running clique finder)
        i.e. add clusters in uc to self.uc
        """
        i = max(self.uc) + 1
        for k, v in uc.iteritems():
            cid = k + i
            self.uc[cid] = v
            self.changes.add(cid)
            # even if it's a size-1/2 cluster, it still needs to
            # be in changes for the dir to be created

    def freeze_d(self, cids=None):
        """
        For any ids not in the current batch set self.d[qID][cID] = 0
        so it will never be kicked out of the cluster
        NOTE: this needs to be recalled every time calc_prob is called
        because it resets certain self.d's
        """
        if cids is not None:
            for cid in cids:
                if len(self.uc[cid]) < self.rerun_gcon_size:
                    continue  # no way it's needed
                for qid in set(self.uc[cid]).difference(self.newids):
                    self.d[qid] = {cid: -0}
        else:
            for cid, qids in self.uc.iteritems():
                if len(self.uc[cid]) < self.rerun_gcon_size:
                    continue  # no way it's needed
                for qid in set(qids).difference(self.newids):
                    self.d[qid] = {cid: -0}

    def calc_cluster_prob(self, force_calc=False):
        """
        Dump all consensus file to ref_consensus.fa --> get SA
        --> run self BLASR
        """
        # make the consensus file & SA
        _todo = set(self.uc.keys()) if force_calc else self.changes
        if len(_todo) == 0:
            return

        # with open('ref_consensus.fa', 'w') as f:
        with open(self.refConsensusFa, 'w') as f:
            for cid in _todo:
                #r = SeqIO.read(open(self.refs[cid]), 'fasta')
                rs = [r for r in FastaReader(self.refs[cid])]
                assert(len(rs) == 1)
                r = rs[0]
                f.write(">{0}\n{1}\n".format(r.name.split()[0],
                                             r.sequence))

        saFN = f.name + ".sa"
        cmd = "sawriter {sa} {fa} -blt 8 -welter".\
            format(sa=real_upath(saFN), fa=real_upath(f.name))
        self.add_log("Creating a suffix array for ref_consensus.fa.",
                     level=logging.INFO)
        description = "Failed to create suffix array."

        time_1 = datetime.now()
        self.run_cmd_and_log(cmd=cmd, description=description)
        time_2 = datetime.now()
        msg = "Total time for creating a saffix array for " + \
              "ref_consensus is {t}".format(t=time_2 - time_1)
        self.add_log(msg, level=logging.INFO)
        blasrFN = f.name + ".blasr"
        if op.exists(blasrFN):
            os.remove(blasrFN)

        cmd = "blasr {qfa} ".format(qfa=real_upath(self.fasta_filename)) + \
              "{tfa} ".format(tfa=real_upath(f.name)) + \
              "-m 5 -bestn 10 -nCandidates 10 -maxLCPLength 15 " + \
              "-nproc {n} ".format(n=self.blasr_nproc) + \
              "-maxScore {s} ".format(s=self.maxScore) + \
              "-sa {sa} -out {o}".format(sa=real_upath(saFN),
                                         o=real_upath(blasrFN))
        msg = "Calling blasr to align input fasta file to ref_consensus"
        self.add_log(msg, level=logging.INFO)

        time_3 = datetime.now()
        self.run_cmd_and_log(cmd)
        time_4 = datetime.now()
        msg = "Total time for aligning input fasta to ref_consensus is {t}".\
              format(t=time_4 - time_3)
        self.add_log(msg, level=logging.INFO)

        msg = "cleaning prob for {n} reads".format(n=len(_todo))
        self.add_log(msg, level=logging.INFO)

        time_5 = datetime.now()
        self.clean_prob_for_cids(_todo)
        time_6 = datetime.now()
        msg = "Total time for cleaning probs for {n} clusters is {t}".\
              format(n=len(_todo), t=time_6 - time_5)
        self.add_log(msg, level=logging.INFO)

        msg = "Calling g to calculate prob form BLASR output."
        self.add_log(msg, level=logging.INFO)

        time_7 = datetime.now()
        self.g(blasrFN)
        time_8 = datetime.now()
        msg = "Total time for calling g is {t}".format(t=time_8 - time_7)

    def g(self, output_filename):
        """
        The meat of processing a BLASR output
        Fills in the self.d
        (REMEMBER to pre-clean the self.d)
        """
        # for qID, cID, qStart, qEnd, _missed_q, _missed_t, fakecigar, _ece_arr
        for hit in blasr_against_ref(
                output_filename=output_filename,
                is_FL=self.is_FL, sID_starts_with_c=True,
                qver_get_func=self.probQV.get_smoothed,
                qv_prob_threshold=self.qv_prob_threshold,
                ece_penalty=self.ece_penalty,
                ece_min_len=self.ece_min_len):

            if hit.qID not in self.d:
                self.d[hit.qID] = {}

            if hit.fakecigar is not None:
                self.d[hit.qID][hit.cID] = self.probQV.calc_prob_from_aln(
                    hit.qID, hit.qStart, hit.qEnd, hit.fakecigar)

    def run_til_end(self, max_iter=99):
        """
        This should only be run on the first round.
        Before add_new_batch() is ever called.

        (1) dump current stuff to tmp pickle
        (2) reassign clusters as needed (call self.onemove())
        (3) re-cluster the orphans
        """
        no_change_count = 0
        iter_count = 1
        while no_change_count < 10 and iter_count <= max_iter:
            self.add_log("", level=logging.INFO)
            self.add_log("run_til_end iteration {n}, global iteration {m}".
                         format(n=iter_count - 1, m=self.global_count),
                         level=logging.INFO)
            time_0 = datetime.now()
            orphans = self.onemove()
            if len(orphans) > 0:
                with open(self.tmpOrphanFa, 'w') as f:
                    for sid in orphans:
                        f.write(">{0}\n{1}\n".
                                format(sid, self.seq_dict[sid].sequence))
                try:
                    # remove tmp.orphan.fa.self.blasr
                    os.remove(self.selfBlasrFN(self.tmpOrphanFa))
                except Exception:
                    pass

                self.add_log("Clustering orphan reads and adding them to uc.")
                time_1 = datetime.now()
                iceinit = IceInit(readsFa=self.tmpOrphanFa,
                                  qver_get_func=self.probQV.get_smoothed,
                                  ice_opts=self.ice_opts,
                                  sge_opts=self.sge_opts)

                uc = iceinit.uc
                self.add_uc(uc)
                time_2 = datetime.now()
                msg = "Total time for clustering orphan reads and adding " + \
                      "new clusters is {t}.".format(t=time_2 - time_1)
                self.add_log(msg, level=logging.INFO)

            time_3 = datetime.now()
            self.add_log("Total time for run_til_end iteration " +
                         "{n}, global iteration {m} is {t}.".
                         format(n=iter_count - 1, m=self.global_count,
                                t=time_3 - time_0), level=logging.INFO)
            iter_count += 1
            self.global_count += 1

            # record the current gcon consensus
            current_gcon_seq_in_changes = {}
            for cid in self.changes:
                if cid in self.refs:
                    current_gcon_seq_in_changes[cid] = \
                        get_the_only_fasta_record(self.refs[cid]).sequence

            self.run_gcon_parallel(self.changes)
            # remove from self.changes ones that did not change
            _cids = set(self.changes)
            for cid in _cids:
                if cid in current_gcon_seq_in_changes:
                    seq = get_the_only_fasta_record(self.refs[cid]).sequence
                    if seq == current_gcon_seq_in_changes[cid]:
                        msg = "REMOVING " + str(cid) + \
                              " from changes because no gcon change"
                        self.add_log(msg)
                        self.changes.remove(cid)
            self.calc_cluster_prob()
            self.freeze_d()
            # see if there are more moves possible
            if self.no_moves_possible():
                self.add_log("No more moves possible. Done!")
                break
            no_change_count += (len(self.changes) == 0)

    def onemove(self):
        """
        (1) if has a better cluster, move to it
        (2) if no bette cluster, move it to orphan group
        """
        self.changes = set()  # always clean up changes first
        orphan = []
        qid_to_cid = {}  # qID --> cluster index
        # make cluster_dict
        for i, cluster in self.uc.iteritems():
            for qID in cluster:
                qid_to_cid[qID] = i

        for qID in self.d:
            old_i = qid_to_cid[qID]
            x = self.d[qID].items()
            if len(x) == 0:
                # no best! move it to the orphan group
                self.remove_from_cluster(qID, old_i)
                orphan.append(qID)
            else:
                x.sort(key=lambda p: p[1], reverse=True)
                best_i, best_i_prob = x[0][0], x[0][1]
                if best_i != qid_to_cid[qID]:
                    # moving assignment from old_i to best_i
                    msg = "best for {0} is {1},{2} (currently: {3}, {4}".\
                        format(qID, best_i, best_i_prob, old_i,
                               (self.d[qID][old_i] if old_i in self.d[qID]
                                else 'None'))
                    self.add_log(msg)

                    # move qID to best_i
                    self.uc[best_i].append(qID)

                    # ToDo: make more flexible
                    # changes were made to from_i and best_i
                    # only re-run gcon if the clusters are small
                    if len(self.uc[best_i]) < self.rerun_gcon_size:
                        self.changes.add(best_i)
                    if len(self.uc[old_i]) < self.rerun_gcon_size:
                        self.changes.add(old_i)
                    self.remove_from_cluster(qID, old_i)
                else:
                    # --------------------------
                    # singletons always have best prob as it self, so treat
                    # specially if there is another cluster with no-zero prob
                    # move to it with some probability
                    # NOTE: here singleton is anything <= 2
                    # (to match criterion used in run_gcon_parallel)
                    # --------------------------
                    if len(self.uc[old_i]) <= 2:
                        #x = filter(lambda (a, b): a != old_i, x)
                        x = [y for y in x if y[0] != old_i]
                        if len(x) > 0 and \
                                random.random() <= self.random_prob:
                            # right now hard-code to 30% prob
                            best_i, best_i_prob = x[0][0], x[0][1]
                            msg = "randomly moving {0} from {1} to {2}".\
                                format(qID, old_i, best_i)
                            self.add_log(msg)

                            self.uc[best_i].append(qID)
                            if len(self.uc[best_i]) < self.rerun_gcon_size:
                                self.changes.add(best_i)
                            self.changes.add(old_i)
                            self.remove_from_cluster(qID, old_i)
        return orphan

    def add_new_batch(self, batch_filename):
        """Add a new batch of sequences to clusters."""
        msg = "Adding a new batch of fasta reads {f} to clusters.".\
              format(f=batch_filename)
        self.add_log(msg, level=logging.INFO)

        # sanity check
        if not op.exists(batch_filename):
            errMsg = "Adding a new batch file {b} which does not exist.".\
                format(b=batch_filename)
            self.add_log(errMsg, level=logging.ERROR)
            raise ValueError(errMsg)

        # assert r.id not in self.d
        for r in FastaReader(batch_filename):
            rid = r.name.split()[0]
            if r.name.split()[0] in self.d:
                errMsg = "new batch file {b} contains a read {r} ".\
                    format(b=batch_filename, r=rid) + \
                    " of an existing cluster."
                self.add_log(errMsg, level=logging.ERROR)
                raise ValueError(errMsg)

        # after this step:
        # self.removed_qids contains ids that were removed from
        # self.uc/self.d
        # self.fasta_filename is still pointing to the current version,
        # not new batch fasta
        self.final_round_before_freeze(0)

        # after this step:
        # active_ids temporarily hold IDs that are still in
        #  self.uc/self.d, but from small clusters
        # later will append them to the latest fasta file so they
        # still can move around
        active_ids = set()
        for cid, ids in self.uc.iteritems():
            if len(ids) >= self.rerun_gcon_size:
                # if not already removed
                msg = "Removing from probQV all members of {0}".format(cid)
                self.add_log(msg)
                self.probQV.remove_ids(self.newids.intersection(ids))
            else:
                active_ids.update(ids)

        # after this step:
        # latest fasta file is changed to
        # {new batch} + {any id that was removed from final round}
        self.fasta_filename = self.currentFa  # "current.fasta"
        cmd = "cp {bat} {cur}".format(bat=real_upath(batch_filename),
                                      cur=real_upath(self.fasta_filename))
        self.run_cmd_and_log(cmd)

        # Append qids removed from last round to current.fasta
        with open(self.fasta_filename, 'a') as f:
            for qid in self.removed_qids:
                f.write(">{0}\n{1}\n".format(qid, self.seq_dict[qid].sequence))

        # after this step:
        # setting newids =  {new batch} + {any id removed from final round}
        # self.d is initialized for everything in newids
        self.newids = set()
        for r in FastaReader(self.fasta_filename):
            rid = r.name.split()[0]
            self.d[rid] = {}
            self.newids.add(rid)

        # convert fasta + ccs --> fastq
        batch_fq = batch_filename[:batch_filename.rfind('.')] + '.fastq'
        ice_fa2fq(batch_filename, self.ccs_fofn, batch_fq)
        # adding {new batch} to probQV
        # now probQV contains
        # {new batch} + {any id removed from final round} + {active ids}
        if self.use_ccs_qv:
            self.probQV.add_seqs_from_fasta(batch_filename)
        else:
            self.probQV.add_seqs_from_fastq(batch_fq)
        # only ids from new batch are not already in probQV

        self.calc_cluster_prob(True)
        orphans = self.add_seq_to_cluster()

        ofa = self.orphanFa(self.fasta_filename)
        # with open(self.fasta_filename + '.orphan.fa','w') as f:
        with open(ofa, 'w') as f:
            for sid in orphans:
                f.write(">{0}\n{1}\n".format(sid, self.seq_dict[sid].sequence))

        # if op.exists(self.fasta_filename + '.orphan.fa.self.blasr'):
        #    os.remove(self.fasta_filename + '.orphan.fa.self.blasr')
        # must clean it in case it already exists
        if op.exists(self.selfBlasrFN(ofa)):
            os.remove(self.selfBlasrFN(ofa))

        # uc=init_ICE.init_cluster_by_clique(self.fasta_filename + '.orphan.fa',
        # self.probQV.get_smoothed, nproc=self.blaserNProc, bestn=200, ...
        iceinit = IceInit(readsFa=ofa,
                          qver_get_func=self.probQV.get_smoothed,
                          ice_opts=self.ice_opts,
                          sge_opts=self.sge_opts)
        uc = iceinit.uc
        self.add_uc(uc)

        self.run_gcon_parallel(self.changes)

        # wait until now to incorporate active_ids to save unncessary BLASR-ing
        # on them since their self.d would thus far remain the same unless
        # it's in self.changes.
        # adding {active ids} to current fasta file
        # now fasta file contains:
        # {new batch} + {any id removed from final round} + {active ids}
        # which is consistent with self.newids and self.probQV
        #f = open(self.fasta_filename, 'a')
        with open(self.fasta_filename, 'a') as f:
            for sid in active_ids:
                f.write(">{0}\n{1}\n".format(sid,
                                             self.seq_dict[sid].sequence))
        self.newids.update(active_ids)

        # sanity check!
        self.ensure_probQV_newid_consistency()

        # update prob for self.newids VS {all changed clusters}
        self.calc_cluster_prob()
        # self.freeze_d()
        msg = "Finished to add a new batch of fasta reads {f} to clusters.".\
              format(f=batch_filename)
        self.add_log(msg, level=logging.INFO)

    def ensure_probQV_newid_consistency(self):
        """
        Ensure that
        self.fasta_filename agrees with self.newids agrees with self.probQV
        """
        # for r in SeqIO.parse(open(self.fasta_filename), 'fasta'):
        with FastaReader(self.fasta_filename) as reader:
            for r in reader:
                rid = r.name.split()[0]
                if rid not in self.newids:
                    self.newids.add(rid)
                    #has_err = False
                    msg = "{0} should be in newids but not. Add it!".format(rid)
                    self.add_log(msg)
                try:
                    self.probQV.get_smoothed(qID=rid, qvname='DeletionQV')
                except KeyError:
                    #has_err = False
                    self.probQV.add_ids_from_fasta(newids=[rid])
                    msg = "{0} should be in probQV but not. Add it!".format(rid)
                    self.add_log(msg)

    def run_for_new_batch(self):
        """Run for the new batch. """
        self.freeze_d()  # run it first in case I forgot
        self.run_til_end(1)
        self.freeze_d()

    def check_cluster_sanity(self, check_dir_lambda=(lambda x: False)):
        """Check cluster sanity."""
        _membership = {}
        try:
            for cid, members in self.uc.iteritems():
                assert len(members) > 0
                for x in members:
                    assert x not in _membership
                    _membership[x] = cid
                if (cid not in self.refs or
                        len(self.refs[cid]) == 0):
                    print "ref for {0} does not exist!".format(cid)
                elif check_dir_lambda(cid):  # or random.random() <= .01:
                    self.add_log("Randomly checking {cid}".format(cid))
                    seqids = set(line.strip()[1:].split()[0] for line in
                                 os.popen("grep \">\" " + self.clusterInFa(cid)))
                    assert seqids == set(members)
                    assert op.exists(self.refs[cid])
            for x in self.d:
                if len(self.d[x]) == 1 and self.d[x].values()[0] == 0:
                    cid = self.d[x].keys()[0]
                    assert len(self.uc[cid]) >= self.rerun_gcon_size
        except AssertionError:
            errMsg = "Cluster sanity check failed!"
            self.add_log(errMsg, level=logging.ERROR)
            raise ValueError(errMsg)
        return _membership

    def keep_adding_files(self, sizes):
        """
        Iteratively add new files in self.fasta_filenames_to_add
        (1) merge for at most 3 rounds
        (2) add new batch
        (3) cluster reassignments (run_for_new_batch)
        """
        # for f in files:
        while (len(self.fasta_filenames_to_add) > 0):
            f = self.fasta_filenames_to_add.pop(0)
            self.add_log("adding file {f}".format(f=f))

            self.run_post_ICE_merging(
                consensusFa=self.tmpConsensusFa,
                pickleFN=self.tmpPickleFN,
                max_iter=3)
            # out_prefix='output/tmp',
            self.add_new_batch(f)
            self.check_cluster_sanity()
            for _i in xrange(5):
                self.run_for_new_batch()
                sizes.append(len(self.uc))
            self.write_pickle(self.uptoPickleFN(f))
            #'output/upto_'+f+'.pickle')
            self.write_consensus(self.uptoConsensusFa(f))
            #'output/upto_'+f+'.consensus.fa')

    def run_post_ICE_merging(self, consensusFa, pickleFN, max_iter):
        """
        (1) write pickle/consensus file
        (2) find mergeable clusters
        (3) run gcon on all merged clusters
        """
        consensus_filename = consensusFa
        pickle_filename = pickleFN
        #consensus_filename = out_prefix + '.consensus.fa'
        #pickle_filename = out_prefix + '.pickle'
        #self._changes = set(self.changes)
        # this is just back up for debugging purpose
        for _i in xrange(max_iter):
            self.add_log("Running post-iterative-merging iterate {n}".
                         format(n=_i), level=logging.INFO)
            self.changes = set()
            self.add_log("Writing pickle file: " + pickle_filename)
            self.write_pickle(pickle_filename)

            self.add_log("Writing consensus file: " + consensus_filename)
            self.write_consensus(consensus_filename)

            iters = self.find_mergeable_consensus(consensus_filename)

            for r in iters:
                self.do_icec_merge_nogcon(r)

            if len(self.changes) == 0:
                self.add_log("No more merges, break!")
                break
            self.run_gcon_parallel(self.changes)
            self.calc_cluster_prob()
            self.freeze_d()

    def find_mergeable_consensus(self, fasta_filename):
        """
        run self-blasr on input fasta (likely tmp.consensus.fa)
        and yield BLASRM5 record of mergeable clusters
        """
        out = self.selfBlasrFN(fasta_filename)
        if op.exists(out):  # clean out the blasr file from the last run
            os.remove(out)
        cmd = "blasr {i} {i} ".format(i=real_upath(fasta_filename)) + \
              "-bestn 10 -nCandidates 10 -minPctIdentity 99 -m 5 " + \
              "-maxScore {s} ".format(s=self.maxScore) + \
              "-nproc {cpu} -out {o}".format(cpu=self.blasr_nproc,
                                             o=real_upath(out))
        self.add_log("Calling blasr to self align " + fasta_filename)
        self.run_cmd_and_log(cmd)

        for r in BLASRM5Reader(out):
            if possible_merge(r=r, ece_penalty=self.ece_penalty,
                              ece_min_len=self.ece_min_len):
                yield r

    def do_icec_merge_nogcon(self, r):
        """
        r --- BLASRM5Record
        If cluster i and j can merge, create a new cluster k, delete i and j,
        add k to changes, adn return k.
        Delay gcon till later (as k is in self.changes).
        """
        self.add_log("do_icec_merge_nogcon")
        # qID: c0/7/3942
        # sID: c1/8/3941
        i = int(r.qID.split('/')[0][1:])
        j = int(r.sID.split('/')[0][1:])
        if i == j:
            return None
        if i not in self.uc or j not in self.uc:
            msg = "Cluster {0} or {1} no longer exist.".format(i, j)
            self.add_log(msg)
            return None
        else:
            msg = "Merging clusters {0} and {1}".format(r.qID, r.sID)
            self.add_log(msg)
            k = self.make_new_cluster()
            self.uc[k] = self.uc[i] + self.uc[j]
            self.delete_cluster(i)
            self.delete_cluster(j)
            self.freeze_d([k])
            self.changes.add(k)
            return k

    @property
    def report_fn(self):
        """Return cluster_report.FL.csv"""
        return op.join(self.out_dir, "cluster_report.FL.csv")

    def run(self):
        """
        Major API provided to run IceIterative after IceInit is done.
        First, reads_of_insert.fasta will be splitted into many chunks,
        We will work on the very first chunk in the beginning, we will
        call IceInit to create perfect cliques and to build probability
        model.
        Second, after IceInit is done, we call IceIterative to take
        clusters and probModel created by IceInit as input.
        Finally, we call IceIterative.run() to iteratively add all the
        remaining fasta file chunks to cluster.
        e.g.,

        # split roi_fasta into splittedFas: [f0, f2, ..., f9]
        iceinit = IceInit(f0)
        obj = IceIterative(fasta_filename=f0,
                           fasta_filenames_to_add=[f2,...,f9],
                           all_fasta_filename=roi_fasta,
                           fofn=roi_fofn,
                           ice_opts=ice_opts, sge_opts=sge_opts,
                           uc=iceinit.uc,
                           ...
                           )
        obj.run()

        """
        msg = "IceIterative run."
        self.add_log(msg, level=logging.INFO)

        msg = "First five run of run_til_end()."
        self.add_log(msg, level=logging.INFO)
        self.run_til_end(5)
        sizes = [len(self.uc)]

        msg = "Adding new reads to constructed clusters."
        self.add_log(msg, level=logging.INFO)
        self.keep_adding_files(sizes=sizes)

        msg = "Merging clusters."
        self.add_log(msg, level=logging.INFO)
        self.run_post_ICE_merging(consensusFa=self.tmpConsensusFa,
                                  pickleFN=self.tmpPickleFN,
                                  max_iter=3)

        msg = "Finalize clusters."
        self.add_log(msg, level=logging.INFO)
        # Change random probability threshold to a very small value
        # so that the chance of taking a move in run_til_end -> onemove
        # is low.
        self.random_prob = 0.01

        for _i in xrange(5):
            self.add_log("run_for_new_batch iteration {n}".format(n=_i),
                         level=logging.INFO)
            self.run_for_new_batch()
            sizes.append(len(self.uc))

        msg = "Cluster size history: {sizes}".format(
            sizes=', '.join([str(s) for s in sizes]))
        self.add_log(msg, level=logging.INFO)

       # Write final pickle
        self.write_final_pickle()

        # write final consensus.fa and final_consensus.fa.sa
        self.write_final_consensus()

        # Write a csv report: line = read cluster
        self.add_log("Writing a csv report of cluster -> FL reads to {f}".
                     format(f=self.report_fn), level=logging.INFO)
        self.write_report(report_fn=self.report_fn, uc=self.uc)

        msg = "IceIterative completed."
        self.add_log(msg, level=logging.INFO)

        # Close log file.
        self.close_log()
