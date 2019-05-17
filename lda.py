#! /usr/bin/env python3
"""
A module to provide functions for the LDA (machine learning) validation
of PSMS.

"""
import copy
from typing import Dict, List, Optional, Tuple
import warnings

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis as LDA
from sklearn.exceptions import DataConversionWarning
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from peptide_spectrum_match import PSM
import proteolysis
from psm_container import PSMContainer

# Silence this since it arises when converting ints to float in StandardScaler
warnings.filterwarnings(action='ignore', category=DataConversionWarning)


class CustomPipeline(Pipeline):
    """
    A simple subclass of sklearn's Pipeline to provide a convenience function
    to return the combined results of multiple base class methods.

    """
    def decide_predict(self, X):
        """
        Calls the decision_function and predict methods of the base class.

        """
        return np.transpose([
            self.decision_function(X),
            self.predict(X)
        ])
        
        
def calculate_fisher_score(xvals1: np.array, xvals2: np.array) -> float:
    """
    """
    return ((xvals1.mean() - xvals2.mean()) ** 2) / \
           (xvals1.std() ** 2 + xvals2.std() ** 2)


class FisherScoreSelector():
    """
    A custom feature selector for use with sklearn.Pipeline. This selector
    will extract the features whose Fisher scores exceed the specified
    threshold.

    """
    def __init__(self, threshold: float):
        """
        Initialize the selector with the desired score threshold.

        Args:
            threshold (float): The minimum Fisher score to retain features.

        """
        # The Fisher score threshold by which to filter features.
        self.threshold = threshold

        # A list containing dictionaries of feature to score. This is used
        # to track scores across CV folds and report the averages.
        self._scores: List[Dict[str, float]] = []
        # A list containing the features to be used in transformation.
        self._features: List[str] = []

    def transform(self, X):
        """
        Transform the input by extracting the data for the features whose
        Fisher scores exceeded the threshold.

        """
        return X[self._features]

    def fit(self, X, y):
        """
        Fit the selector by calculating the Fisher scores for each feature
        and storing those which exceed the threshold.

        """
        self._features = []
        scores = {}
        for col in X.columns:
            vals1 = X[col].loc[y]
            vals2 = X[col].loc[~y]
            score = calculate_fisher_score(vals1, vals2)
            if score >= self.threshold:
                self._features.append(col)
            scores[col] = score
        self._scores.append(scores)
        return self

    def get_average_scores(self) -> Dict[str, float]:
        """
        Calculates the average Fisher score (across any CV folds) for each
        feature.

        Returns:
            dictionary of feature to average score.

        """
        merged = {k: [d[k] for d in self._scores]
                  for k in self._scores[0].keys()}
        return {k: sum(v) / len(v) for k, v in merged.items()}

    def get_average_selected_features(self) -> List[str]:
        """
        Finds the features which, using their average Fisher scores, are
        selected according to the given threshold.

        Returns:
            List of feature names.

        """
        avg_scores = self.get_average_scores()
        return [k for k, v in avg_scores.items() if v >= self.threshold]

    def __str__(self) -> str:
        """
        Implements the string conversion.

        """
        return f"<{self.__class__.__name__} {self.__dict__}>"


def _lda_pipeline(fisher_threshold: float = None):
    """
    Constructs an instance of CustomPipeline using the LDA classifier,
    standard scaling and Fisher score selection, if the fisher_threshold
    is provided.

    """
    steps = [] if fisher_threshold is None else [
        # Fisher score selection of features is included in the piepline
        # since it should be performed independently on each cross
        # validation fold
        ("fisher_selection", FisherScoreSelector(fisher_threshold))
    ]
    steps.extend([
        # Scale the features to have mean 0 and unit variance
        ("scaler", StandardScaler()),
        # Perform LDA
        ("lda", LDA())
    ])
    return CustomPipeline(steps)


def _get_dist_stats(classes, preds, scores) -> Dict[int, Tuple[float, float]]:
    """
    Calculates the distribution statistics (mean and std) for the
    score distribution of each class.

    """
    return {int(cl): (np.mean(scores[preds == cl]),
                      np.std(scores[preds == cl]))
            for cl in classes}


def calculate_probs(classes, preds, scores):
    """
    Calculates the normal distribution probabilities for the predictions.

    Args:
        classes (list): The possible categorization classes.
        preds (list): The class predictions.
        scores (list): The corresponding prediction scores.

    Returns:
        Dictionary mapping class to prediction probabilities.

    """
    # Calculate the mean and standard deviation for each class
    stats = _get_dist_stats(classes, preds, scores)

    probs = {}
    # Calculate probabilities based on the normal distribution
    for _class in classes:
        _class = int(_class)
        probs[_class] = \
            norm.pdf(scores, stats[_class][0], stats[_class][1]) /\
            sum(norm.pdf(scores, mean, std) for (mean, std) in stats.values())

    return probs, stats


def calculate_prob(_class: int, score: float, dist_stats) -> float:
    """
    Calculates the normal distribution probability for a single LDA score.

    Args:
        _class (int): The class ID.
        score (float): The determined LDA score.
        dist_stats (dict): A dictionary to tuple of (mean, st. dev) keyed by
                           class.

    Returns:
        The corresponding probability as a float.

    """
    return norm.pdf(score, dist_stats[_class][0], dist_stats[_class][1]) /\
        sum(norm.pdf(score, mean, std) for (mean, std) in dist_stats.values())


def calculate_score(prob: float, dist_scores) -> float:
    """
    Solves for the score corresponding to the given probability.

    Args:
        prob (float)

    Returns:
        float

    """
    m_t, s_t = dist_scores[1]
    m_d, s_d = dist_scores[0]

    a = - 2. * s_d * s_d + 2. * s_t * s_t
    b = 4. * m_t * s_d * s_d - 4. * m_d * s_t * s_t
    c = (- 2. * s_d * s_d * m_t * m_t + 2. * s_t * s_t * m_d * m_d -
         (4. * s_d * s_d * s_t * s_t) *
         (np.log(prob / s_d) - np.log((1 - prob) / s_t)))

    x1 = (-b + np.sqrt((b * b) - (4 * a * c))) / (2 * a)
    #x2 = (-b - np.sqrt((b * b) - (4 * a * c))) / (2 * a)

    return x1


def lda_model(df: pd.DataFrame, features: List[str],
              prob_threshold: float = 0.99)\
        -> Tuple[CustomPipeline, Dict[int, Tuple[float, float]], float]:
    """
    Trains and uses an LDA validation model.

    Args:
        df (pandas.DataFrame): The features and target labels for the PSMs.
        features (list): The names of the feature columns.

    Returns:

    """
    X = df[features]
    y = df["target"].astype("bool")

    pipeline = _lda_pipeline()

    # Train the model on the whole data set
    model = pipeline.fit(X, y)
    scores = model.decision_function(X)
    preds = model.predict(X)

    score_stats = _get_dist_stats(y.unique(), preds, scores)

    full_lda_threshold = calculate_score(prob_threshold, score_stats)

    return pipeline, score_stats, full_lda_threshold


def _lda_validate(df: pd.DataFrame, features: List[str],
                  full_lda_threshold: float,
                  prob_threshold: float = 0.99, cv: int = 10)\
        -> Tuple[float, pd.DataFrame, CustomPipeline]:
    """
    Trains and uses an LDA validation model using cross-validation.

    Args:
        df (pandas.DataFrame): The features and target labels for the PSMs.
        features (list): The names of the feature columns.
        full_lda_threshold (float):
        cv (int, optional): If None, no cross validation will be used.
                            Otherwise, this should be an integer for the
                            number of folds.

    Returns:

    """
    X = df[features]
    y = df["target"].astype("bool")

    pipeline = _lda_pipeline()

    skf = StratifiedKFold(n_splits=cv)
    results = np.zeros((len(X), 3))
    for train_idx, test_idx in skf.split(X, y):
        model = pipeline.fit(X.iloc[train_idx], y.iloc[train_idx])
        X_test = X.iloc[test_idx]
        scores = model.decision_function(X_test)
        preds = model.predict(X_test)

        # Shift the prob_threshold score to match the full distribution
        lda_threshold = calculate_score(
            prob_threshold, _get_dist_stats(y.unique(), preds, scores))
            
        if lda_threshold > 1000:
            print(prob_threshold, _get_dist_stats(y.unique(), preds, scores))
            print(scores)
            print(lda_threshold)

        scores += full_lda_threshold - lda_threshold
        print(full_lda_threshold, lda_threshold)

        results[test_idx, 0], results[test_idx, 1] = scores, preds
        results[test_idx, 2] = calculate_probs(y.unique(), preds, scores)[0][1]

    df["score"], df["prob"] = results[:, 0], results[:, 2]

    return sum(y != results[:, 1]) / len(y), df, pipeline


def lda_validate(df: pd.DataFrame, features: List[str],
                 full_lda_threshold: float, **kwargs)\
                 -> Tuple[pd.DataFrame]:
    """
    Trains and uses an LDA validation model using cross-validation.

    Args:
        df (pandas.DataFrame): The features and target labels for the PSMs.
        features (list): The names of the feature columns.

    Returns:

    """
    _, results, _ =\
        _lda_validate(df, features, full_lda_threshold, **kwargs)
    return results


def merge_lda_results(psms: List[PSM], lda_results) -> List[PSM]:
    """
    Merges the LDA results (score and probability) to the PSM objects.

    Args:
        psms (list of PSMs): The PSM list to filter.
        lda_results (pandas.DataFrame): The LDA validation results.

    Returns:
        PSMs with their lda_score and lda_prob attributes set.

    """
    lda_results["uid"] = lda_results.data_id + "_" + lda_results.spec_id + \
        "_" + lda_results.seq
    for psm in psms:
        idx = lda_results.index[
            (lda_results.target) & (lda_results.uid == psm.uid)].tolist()[0]
        trow = lda_results.loc[idx]
        drow = lda_results.loc[idx + 1]
        psm.lda_score, psm.lda_prob = trow.score, trow.prob
        psm.decoy_lda_score, psm.decoy_lda_prob = drow.score, drow.prob

    return psms


def calculate_scores(model: CustomPipeline, psms: List[PSM],
                     features: List[str], target_only: bool = True):
    """
    Calculates the LDA scores for the given psms using the trained model.

    Args:
        model (sklearn model): A trained sklearn model.
        psms (list/PSMContainer): The PSMs to predict.
        features (list): The list of features to use for predictions.

    Returns:
        The LDA scores for the PSMs as a numpy array.

    """
    return model.decide_predict(
        PSMContainer(psms).to_df(target_only)[features])[:, 0]


def apply_deamidation_correction(pipeline: CustomPipeline, dist_stats,
                                 psms: List[PSM],
                                 features: List[str],
                                 target_mod: Optional[str],
                                 proteolyzer: proteolysis.Proteolyzer):
    """
    Removes the deamidation modification from applicable peptide
    identifications and revalides using the trained LDA model. If the score
    for the non-deamidated analogue is greater than that for the deamidated
    peptide, the non-deamidated analogue is assigned as the peptide match for
    that spectrum.

    Args:
        pipeline (sklearn.Pipeline): The trained LDA pipeline.
        psms (list): The validated PSMs.
        target_mod (str): The modification under validation.
        proteolyzer (proteolysis.Proteolyzer)

    Returns:
        The input list of PSMs, with deamidated PSMs replaced by their
        non-deamidated counterparts if their LDA scores are higher.

    """
    for ii, psm in enumerate(psms):
        if not any(ms.mod == "Deamidated" for ms in psm.mods):
            continue
        nondeam_psm = copy.deepcopy(psm)
        nondeam_psm.peptide.mods = [ms for ms in nondeam_psm.mods
                                    if ms.mod != "Deamidated"]
        # Calculate new features
        nondeam_psm.extract_features(target_mod, proteolyzer)

        # Compare probabilities based on the same distribution
        # (from the model trained on the full set of data)
        scores = calculate_scores(
            pipeline, [nondeam_psm, psm], features, target_only=True)
        nondeam_score, score = scores[0], scores[1]
        nondeam_prob = calculate_prob(1, nondeam_score, dist_stats)
        prob = calculate_prob(1, score, dist_stats)

        # Keep the PSM with the highest probability
        if nondeam_prob > prob:
            nondeam_psm.corrected = True

            nondeam_psm.lda_score = nondeam_score
            nondeam_psm.lda_prob = nondeam_prob

            # Reset the PSM validation attributes back to None
            for attr in ["decoy_lda_score", "decoy_lda_prob"]:
                setattr(nondeam_psm, attr, None)

            psms[ii] = nondeam_psm

    return psms
