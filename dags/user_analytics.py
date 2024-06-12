import os
import shutil
from datetime import datetime, timedelta

import boto3
import duckdb
from airflow import DAG
from airflow.decorators import task
from airflow.operators.bash_operator import BashOperator
from airflow.operators.python_operator import PythonOperator
from airflow.providers.amazon.aws.operators.s3 import S3CreateBucketOperator
from airflow.providers.amazon.aws.transfers.local_to_s3 import \
    LocalFilesystemToS3Operator
from airflow.providers.amazon.aws.transfers.sql_to_s3 import SqlToS3Operator
from airflow.providers.apache.spark.operators.spark_submit import \
    SparkSubmitOperator


def get_s3_folder(s3_bucket, s3_folder, local_folder="./temp/s3folder/"):
    s3 = boto3.resource(
        service_name="s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="minio",
        aws_secret_access_key="minio123",
        region_name="us-east-1",
    )
    bucket = s3.Bucket(s3_bucket)
    local_path = os.path.join(local_folder, s3_folder)
    # Delete the local folder if it exists
    if os.path.exists(local_path):
        shutil.rmtree(local_path)

    for obj in bucket.objects.filter(Prefix=s3_folder):
        target = os.path.join(local_path, os.path.relpath(obj.key, s3_folder))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        bucket.download_file(obj.key, target)
        print(f"Downloaded {obj.key} to {target}")


with DAG(
    "user_analytics_dag",
    description="A DAG to Pull user data and movie review data \
        to analyze their behaviour",
    schedule_interval=timedelta(days=1),
    start_date=datetime(2023, 1, 1),
    catchup=False,
) as dag:
    user_analytics_bucket = "user-analytics"

    # Copy data from local ./data to bucket under raw

    # Run Spark job to pull data from the above location
    # process it and write to clean

    # DuckDB to pull data from clean and create a table
    create_s3_bucket = S3CreateBucketOperator(
        task_id="create_s3_bucket", bucket_name=user_analytics_bucket
    )
    movie_review_to_s3 = LocalFilesystemToS3Operator(
        task_id="create_local_to_s3_job",
        filename="/opt/airflow/data/movie_review.csv",
        dest_key="raw/movie_review.csv",
        dest_bucket=user_analytics_bucket,
        replace=True,
    )

    user_purchase_to_s3 = SqlToS3Operator(
        task_id="database_to_s3",
        sql_conn_id="postgres_default",
        query="select * from retail.user_purchase",
        s3_bucket=user_analytics_bucket,
        s3_key="raw/user_purchase/user_purchase.csv",
        replace=True,
    )

    # movie_classifier = SparkSubmitOperator(
    #     task_id="python_job",
    #     conn_id="spark-conn",
    #     application="./dags/scripts/spark/random_text_classification.py",
    # )

    movie_classifier = BashOperator(
        task_id="pyspark",
        bash_command="python $AIRFLOW_HOME/dags/scripts/spark/random_text_classification.py",
    )

    get_movie_review_to_warehouse = PythonOperator(
        task_id="get_movie_review_to_warehouse",
        python_callable=get_s3_folder,
        op_kwargs={"s3_bucket": "user-analytics", "s3_folder": "clean/movie_review"},
    )

    get_user_purchase_to_warehouse = PythonOperator(
        task_id="get_user_purchase_to_warehouse",
        python_callable=get_s3_folder,
        op_kwargs={"s3_bucket": "user-analytics", "s3_folder": "raw/user_purchase"},
    )

    def create_user_behaviour_metric():
        q = """with up as ( select * from '/opt/airflow/temp/s3folder/raw/user_purchase/user_purchase.csv'), mr as (select * from '/opt/airflow/temp/s3folder/clean/movie_review/*.parquet') select up.customer_id, sum(up.quantity * up.unit_price) as amount_spent, sum(case when mr.positive_review then 1 else 0 end) as num_positive_reviews, count(mr.cid) as num_reviews from up join mr on up.customer_id = mr.cid group by up.customer_id
        """
        duckdb.sql(q).write_csv('/opt/airflow/data/behaviour_metrics.csv')
    
    get_user_behaviour_metric = PythonOperator(
        task_id="get_user_behaviour_metric",
        python_callable=create_user_behaviour_metric,
    )

    (
        create_s3_bucket
        >> [movie_review_to_s3, user_purchase_to_s3]
        >> movie_classifier
        >> [get_movie_review_to_warehouse, get_user_purchase_to_warehouse]
        >> get_user_behaviour_metric
    )