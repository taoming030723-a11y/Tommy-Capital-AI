import {REVIEW_VERSION,POLICY_ID} from './technical-review.mjs';
export function reusableTechnical(record,session,{force=false,now=Date.now()}={}){
  return !force&&record?.value.version===REVIEW_VERSION&&record.value.policy_id===POLICY_ID&&record.value.session===session&&
    (record.value.data_complete===true||now-Date.parse(record.updatedAt)<600000);
}
