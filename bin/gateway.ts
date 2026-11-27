import { App } from 'aws-cdk-lib';
import { GatewayStack } from '../lib/gateway-stack';

const app = new App();
new GatewayStack(app, 'AWSCBRace3', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-1' },
});
