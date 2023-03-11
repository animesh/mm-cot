import json
import os
import random
from datetime import datetime

import evaluate
import numpy as np
from torch.utils.data import Dataset
from tqdm import tqdm
from transformers import DataCollatorForSeq2Seq, Seq2SeqTrainer, T5Tokenizer

from src import constants
from src.constants import PromptFormat
from src.data.fakeddit.labels import convert_label_to_int, get_label_text
from src.models.t5_multimodal_generation.training_params import (
    get_t5_model, get_training_args)
from src.models.t5_multimodal_generation.utils import (extract_ans,
                                                       get_backup_dir,
                                                       get_prediction_filename,
                                                       postprocess_text)


class T5ForMultimodalGenerationService:
    seq2seq_trainer = None

    def __init__(self, dataframe, args, tokenizer):
        self.args = args
        self.dataframe = dataframe
        self.save_dir = get_backup_dir(args)
        self.filename = get_prediction_filename(args)
        self.tokenizer = tokenizer or T5Tokenizer.from_pretrained(
            pretrained_model_name_or_path=self.args.model)
        
    def fit(self, train_set, eval_set):
        self.build_seq2seq_base_trainer(train_set, eval_set)
        self.seq2seq_trainer.train()
        self.seq2seq_trainer.save_model(self.save_dir)

    def build_seq2seq_base_trainer(self, train_set, eval_set):
        """
            Build a base seq2seq trainer.
            It is mandatory to run this method if t5 model isn't being trained
        """

        print(f"[Model]: Loading {self.args.model}...\n")
        print("[Data]: Reading data...\n")

        model = get_t5_model(self.args, self.tokenizer, self.save_dir)
        self.model = model

        data_collator = DataCollatorForSeq2Seq(self.tokenizer)
        print("Model parameters: ", model.num_parameters())

        training_args = get_training_args(self.args, self.save_dir)

        self.seq2seq_trainer = Seq2SeqTrainer(
            model=model,
            args=training_args,
            train_dataset=train_set,
            eval_dataset=eval_set,
            data_collator=data_collator,
            tokenizer=self.tokenizer,
            compute_metrics=self.compute_metrics_acc if self.args.prompt_format != constants.PromptFormat.QUESTION_CONTEXT_OPTIONS_LECTURE_SOLUTION.value else self.compute_metrics_rougel
        )

    def evaluate(self, dataset: Dataset):
        """ Generate the textual output for the dataset and computes the metrics """

        output = {
            "metrics": [],
            "predictions": [],
            "targets": []
        }

        for elem in tqdm(dataset):

            out = self.model.generate(
                elem['input_ids'][None, :],
                image_ids=elem['image_ids'][None, :],
            )

            prediction = self.tokenizer.batch_decode(
                out, skip_special_tokens=True,
                clean_up_tokenization_spaces=True
            )

            output["predictions"].extend(prediction)

            label = elem['labels']
            if isinstance(label,list):
                label_decoded = self.tokenizer.batch_decode(
                    label, skip_special_tokens=True,
                    clean_up_tokenization_spaces=True
                )
                label_text = ' '.join(label_decoded)
            else: 
                label_text = get_label_text(convert_label_to_int(label))
            output["targets"].append(label_text)

        output["metrics"] = self._compute_metrics(output["predictions"], output["targets"])

        output_prediction_file = os.path.join(
            self.save_dir, f"predictions_{self.filename}_{datetime.now().strftime(constants.DATE_FORMAT)}.json")

        with open(output_prediction_file, "w") as writer:
            writer.write(json.dumps(output, indent=4))

    def infer(self, sample):
        # Extract EVALUATE common logic in a private method 
        pass

    def train(self, dataset: Dataset):
        # Should use seq2seqTrain ??
        # look at FIT method in this class
        pass
    
    def _seq2seq_existing_check(self):
        if not self.seq2seq_trainer:
            raise NotImplementedError(
                "ERROR T5000001 | Fit model or if model exists build a seq2seq trainer")
        return True

    def _compute_metrics(self, predictions, targets):
        if self.args.prompt_format == PromptFormat.QUESTION_CONTEXT_OPTIONS_LECTURE_SOLUTION.value:
            return self.compute_metrics_rougel(predictions, targets)
        return self.compute_metrics_acc(predictions, targets)

    def compute_metrics_rougel(self, predictions, targets):

        """
        ROUGE-L metric for Rational generation
        """

        metric = evaluate.load("rouge")
        predictions, labels = postprocess_text(
            predictions, targets)

        result = metric.compute(predictions=predictions,
                                references=labels, use_stemmer=True)
        result = {k: round(v * 100, 4) for k, v in result.items()}
        prediction_lens = [np.count_nonzero(
            pred != self.tokenizer.pad_token_id) for pred in predictions]
        result["gen_len"] = np.mean(prediction_lens)
        return {'rouge-l': result}

    def compute_metrics_acc(self, predictions, targets):
        
        """
        Accuracy for Answer inference
        """

        correct = 0
        assert len(predictions) == len(targets)
        for idx, pred in enumerate(predictions):
            reference = targets[idx]
            reference = extract_ans(reference)
            extract_pred = extract_ans(pred)
            best_option = extract_pred
            if reference == best_option:
                correct += 1
        return {'accuracy': float(correct) / len(targets)}
